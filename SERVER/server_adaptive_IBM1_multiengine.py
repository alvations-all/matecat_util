#!/usr/bin/python -u

import sys
import Queue
import threading
import subprocess
import cherrypy
import json
import logging
import re
from itertools import izip
from threading import Timer

from aligner import Aligner_onlineGIZA, Aligner_GIZA, Aligner_IBM1, Aligner_Dummy
from phrase_extractor import Extractor_Moses, Extractor_Dummy
from annotate import Annotator_onlinecache

from ConfigParser import SafeConfigParser

def popen(cmd):
    cmd = cmd.split()
    logger = logging.getLogger('translation_log.popen')
    logger.info("executing: %s" %(" ".join(cmd)))
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

def pclose(pipe):
    def kill_pipe():
        pipe.kill()
    t = Timer(5., kill_pipe)
    t.start()
    pipe.terminate()
    t.cancel()

def init_log(filename):
    logger = logging.getLogger('translation_log')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(filename)
    fh.setLevel(logging.DEBUG)
    logformat = '%(asctime)s %(thread)d - %(filename)s:%(lineno)s: %(message)s'
    formatter = logging.Formatter(logformat)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

class Filter(object):
    def __init__(self, remove_newlines=True, collapse_spaces=True):
        self.filters = []
        if remove_newlines:
            self.filters.append(self.__remove_newlines)
        if collapse_spaces:
            self.filters.append(self.__collapse_spaces)

    def filter(self, s):
        for f in self.filters:
            s = f(s)
        return s

    def __remove_newlines(self, s):
        s = s.replace('\r\n',' ')
        s = s.replace('\n',' ')
        return s       

    def __collapse_spaces(self, s):
        return re.sub('\s\s+', ' ', s)

class WriteThread(threading.Thread):
    def __init__(self, p_in, source_queue, web_queue, name):
        threading.Thread.__init__(self)
        self.pipe = p_in
        self.source_queue = source_queue
        self.web_queue = web_queue
        self.logger = logging.getLogger('translation_log.writer')
        self.name = name

    def run(self):
        i = 0
        while True:
            result_queue, segid, source = self.source_queue.get()
            self.web_queue.put( (i, self.name, result_queue) )
            wrapped_src = u"<seg id=%s>%s</seg>\n" %(i, source)
            self.log("NAME: |%s|" %repr(self.name))
            self.log("writing to process: %s" %repr(wrapped_src))
            self.log("writing to process: segid:%s" %repr(segid))
            self.pipe.write(wrapped_src.encode("utf-8"))
            self.pipe.flush()
            i += 1
            self.source_queue.task_done()

    def log(self, message):
        self.logger.info(message)

class ReadThread(threading.Thread):
    def __init__(self, p_out, web_queue):
        threading.Thread.__init__(self)
        self.pipe = p_out
        self.web_queue = web_queue
        self.logger = logging.getLogger('translation_log.reader')

    def run(self):
        result_queues = []
        while True:
            line = self.pipe.readline() # blocking read
            while not self.web_queue.empty():
                result_queues.append(self.web_queue.get())
            if line == '':
                assert self.web_queue.empty(), "still waiting for answers\n"
                assert result_queues, "unanswered requests\n"
                return
            line = line.decode("utf-8").rstrip().split(" ", 1)
            self.log("reader read: %s" %repr(line))

            found = False
            self.log("looking for id %s in %s result queues" %(line[0], len(result_queues)))
            ###self.log("the rest of the line is :|%s|" %(line[1]))
            for idx, (i, name, q) in enumerate(result_queues):
                if i == int(line[0]):
                    if len(line) > 1:
                        q.put( (name, line[1]) )
                    else:
                        q.put( (name, "") )
                    result_queues.pop(idx)
                    found = True
                    break
            assert found, "id %s not found!\n" %(line[0])

    def log(self, message):
        self.logger.info(message)

class UpdaterProc(object):
    def __init__(self, config):
        # parse config file
        parser = SafeConfigParser()
        parser.read(config)

	self.Aligner_object = Aligner_IBM1(parser)
	self.Extractor_object = Extractor_Moses(parser)
	self.Annotator_object = Annotator_onlinecache(parser)
        self.logger = logging.getLogger('translation_log.updater')

    def update(self, source="", target=""):
        # get alignment information for the (source,correction)
        self.log("ALIGNER_INPUT source: "+str(source))
        self.log("ALIGNER_INPUT correction: "+str(target))
        aligner_output = self.Aligner_object.align(source=source,correction=target)
        self.log("ALIGNER_OUTPUT: "+str(aligner_output))

        # get phrase pairs form the alignment information
        bias, new, full = self.Extractor_object.extract_phrases(source,target,aligner_output)
        self.log("BIAS: "+str(bias))
        self.log("NEW: "+str(new))
        self.log("FULL: "+str(full))

        self.Annotator_object.cbtm_update(new=new, bias=bias, full=full)
        self.Annotator_object.cblm_update(target)

        # read and annotate the next sentence
        dummy_source = ""
        annotated_source = self.Annotator_object.annotate(dummy_source)

	return annotated_source

    def reset(self):
        annotated_source = ''
        annotated_source = annotated_source + '<dlt cblm-command="clear"/>'
        annotated_source = annotated_source + '<dlt cbtm-command="clear"/>'
	return annotated_source

    def log(self, message):
        self.logger.info(message)

class MosesProc(object):
    def __init__(self, cmd, name):
        self.proc = popen(cmd)
        self.logger = logging.getLogger('translation_log.moses')
        self.name = name

        self.source_queue = Queue.Queue()
        self.web_queue = Queue.Queue()

        self.writer = WriteThread(self.proc.stdin, self.source_queue, self.web_queue, self.name)
        self.writer.setDaemon(True)
        self.writer.start()

        self.reader = ReadThread(self.proc.stdout, self.web_queue)
        self.reader.setDaemon(True)
        self.reader.start()

    def close(self):
        self.source_queue.join() # wait until all items in source_queue are processed
        self.proc.stdin.close()
        self.proc.wait()
        self.log("source_queue empty: %s" %self.source_queue.empty())

    def log(self, message):
        self.logger.info(message)

def json_error(status, message, traceback, version):
    err = {"status":status, "message":message, "traceback":traceback, "version":version}
    return json.dumps(err, sort_keys=True, indent=4)

class Root(object):
    required_params_translate = ["q", "key", "target", "source"]
    required_params_update = ["key", "segment", "translation", "target", "source"]
    required_params_reset = ["key"]

    def __init__(self, moses, updater_config, prepro_cmd=None, postpro_cmd=None,
		 sentence_confidence_cmd=None,
		 updater_source_prepro_cmd=None, updater_target_prepro_cmd=None,
		 slang=None, tlang=None, pretty=False, persistent_processes=False,
                 segid_system_map="",
                 verbose=0,
		 timeout=-1):
        self.filter = Filter(remove_newlines=True, collapse_spaces=True)

        self.engines_N = len(moses.keys())
        self.log("_get_engine_key self.engines_N:|%d|" % self.engines_N)

        self.queue_translate = {}
        self.system_name = []
        for k in sorted(moses.keys()):
            self.queue_translate[k] = moses[k].source_queue
            self.system_name.append(k)

        self.updater = {}
        for k in updater_config.keys():
            self.updater[k] = None
            if updater_config[k] != None and updater_config[k] != "_NOUPDATE_":
            	self.updater[k] = UpdaterProc(updater_config[k])

        self.prepro_cmd = []
        if prepro_cmd != None:
            self.prepro_cmd = prepro_cmd

        self.sentence_confidence_cmd = []
        self.sentence_confidence_enabled = 0
        if sentence_confidence_cmd != None:
            self.sentence_confidence_cmd = sentence_confidence_cmd
            self.sentence_confidence_enabled = 1
        self.log("sentence_confidence_enabled: %s" % repr(self.sentence_confidence_enabled))

        self.postpro_cmd = []
        if postpro_cmd != None:
            self.postpro_cmd = postpro_cmd

        self.updater_source_prepro_cmd = []
        if updater_source_prepro_cmd != None:
            self.updater_source_prepro_cmd = updater_source_prepro_cmd

        self.updater_target_prepro_cmd = []
        if updater_target_prepro_cmd != None:
            self.updater_target_prepro_cmd = updater_target_prepro_cmd

        self.expected_params_translate = {}
        self.expected_params_update = {}
        self.expected_params_reset = {}
        if slang:
	    slang_lower = slang.lower()
            self.expected_params_translate['source'] = slang_lower
            self.expected_params_update['source'] = slang_lower
            self.expected_params_reset['source'] = slang_lower
        if tlang:
	    tlang_lower = tlang.lower()
            self.expected_params_translate['target'] = tlang_lower
            self.expected_params_update['target'] = tlang_lower
            self.expected_params_reset['target'] = tlang_lower
        self.persist = bool(persistent_processes)
        self.pretty = bool(pretty)
        self.timeout = timeout
        self.verbose = verbose

        self.segid_engine_map = None
        self.segid_updater_map = None
        if segid_system_map != "":
            self.log("Reading map from segment IDs to engine and updater names: |%s|" % segid_system_map)
            self._set_segment2system_map(segid_system_map)
        else:
            self.log("No map from segment IDs to engine and updater names is available")

    def _set_segment2system_map(self, file):

        map = open(file, 'r')
#format of each line
#segment_id system_name

        self.segid_engine_map = {}
        self.segid_updater_map = {}
        line = map.readline().strip()   
        while line:
            entries = line.split()
            self.segid_engine_map[str(int(entries[0]))] = entries[1]
            self.segid_updater_map[str(int(entries[0]))] = entries[1]
            line = map.readline().strip()       

    def _get_engine_key(self, segid):
        if self.segid_engine_map != None:
            if not segid in self.segid_engine_map:
                 self.log("Segment id is not present in the map. Use the first available engine")
                 name = self.system_name[0]
            else:
                 name = self.segid_engine_map[segid]
        else:
            idx = int(segid) % self.engines_N
            name = self.system_name[idx]
        self.log("Engine name for segid %s is %s" % (str(segid), name ))

        return name

    def _get_updater_key(self, segid):
        if self.segid_updater_map != None:
            if not segid in self.segid_updater_map:
                 self.log("Segment id is not present in the map. Use the first available updater")
                 name = self.updater_name[0]
            else:
                 name = self.segid_updater_map[segid]
        else:
            idx = int(segid) % self.updater_N
            name = self.updater_name[idx]
        self.log("Updater name for segid %s is %s" % (str(segid), name ))

        return name

    def _check_params_translate(self, params):
        return self._check_params(params, self.required_params_translate, self.expected_params_translate)

    def _check_params_update(self, params):
        return self._check_params(params, self.required_params_update, self.expected_params_update)

    def _check_params_reset(self, params):
        return self._check_params(params, self.required_params_reset, self.expected_params_reset)
   
    def _check_params(self, params, required_params, expected_params):
        errors = []
        missing = [p for p in required_params if not p in params]
        if missing:
            for p in missing:
                errors.append({"domain":"global",
                               "reason":"required",
                               "message":"Required parameter: %s" %p,
                               "locationType": "parameter",
                               "location": "%s" %p})
            return {"error": {"errors":errors,
                              "code":400,
                              "message":"Required parameter: %s" %missing[0]}}

        for key, val in expected_params.iteritems():
            assert key in params, "expected param %s" %key
            if params[key].lower() != val:
                message = "expected value for parameter %s:'%s'" %(key,val)
                errors.append({"domain":"global",
                               "reason":"invalid value: '%s'" %params[key],
                               "message":message,
                               "locationType": "parameter",
                               "location": "%s" %p})
                return {"error": {"errors":errors,
                                  "code":400,
                                  "message":message}}
        return None

    def _timeout_error(self, q, location):
        errors = [{"originalquery":q, "location" : location}]
        message = "Timeout after %ss" %self.timeout
        return {"error": {"errors":errors, "code":400, "message":message}}

    def _pipe(self, proc, s):
        u_string = u"%s\n" %s
        proc.stdin.write(u_string.encode("utf-8"))
        proc.stdin.flush()
        return proc.stdout.readline().decode("utf-8").rstrip()

    def _prepro(self, query):
        if not self.persist or not hasattr(cherrypy.thread_data, 'prepro'):
            if hasattr(cherrypy.thread_data, 'prepro'):
                map(pclose, cherrypy.thread_data.prepro)
            cherrypy.thread_data.prepro = map(popen, self.prepro_cmd)
        for proc, cmd in izip(cherrypy.thread_data.prepro, self.prepro_cmd):
            query = self._pipe(proc, query)
        return query

    def _postpro(self, query):
        if not self.persist or not hasattr(cherrypy.thread_data, 'postpro'):
            if hasattr(cherrypy.thread_data, 'postpro'):
                map(pclose, cherrypy.thread_data.postpro)
            cherrypy.thread_data.postpro = map(popen, self.postpro_cmd)
        for proc in cherrypy.thread_data.postpro:
            query = self._pipe(proc, query)
        return query

    def _get_sentence_confidence(self, id, source, target):
        ## force sentence_confidence to be persistent
        if not self.persist or not hasattr(cherrypy.thread_data, 'sentence_confidence'):
            if hasattr(cherrypy.thread_data, 'sentence_confidence'):
                map(pclose, cherrypy.thread_data.sentence_confidence)
            cherrypy.thread_data.sentence_confidence = map(popen, self.sentence_confidence_cmd)

        pattern = "<seg id=\".*?\">(?P<key>.+?)<\/seg>"
        re_match = re.compile(pattern)

        input = "<seg id=\""+id+"\"><src>"+source+"</src><trg>"+target+"</trg></seg>"
        output = input
        for proc in cherrypy.thread_data.sentence_confidence:
            output = self._pipe(proc, output)

        m = re_match.search(output)
        value = m.group('key')
        
        return value

    def _updater_source_prepro(self, query):
        if not self.persist or not hasattr(cherrypy.thread_data, 'updater_source_prepro'):
            if hasattr(cherrypy.thread_data, 'updater_source_prepro'):
                map(pclose, cherrypy.thread_data.updater_source_prepro)
            cherrypy.thread_data.updater_source_prepro = map(popen, self.updater_source_prepro_cmd)
        for proc, cmd in izip(cherrypy.thread_data.updater_source_prepro, self.updater_source_prepro_cmd):
            query = self._pipe(proc, query)
        return query

    def _updater_target_prepro(self, query):
        if not self.persist or not hasattr(cherrypy.thread_data, 'updater_target_prepro'):
            if hasattr(cherrypy.thread_data, 'updater_target_prepro'):
                map(pclose, cherrypy.thread_data.updater_target_prepro)
            cherrypy.thread_data.updater_target_prepro = map(popen, self.updater_target_prepro_cmd)
        for proc, cmd in izip(cherrypy.thread_data.updater_target_prepro, self.updater_target_prepro_cmd):
            query = self._pipe(proc, query)
        return query

    def _getOnlyTranslation(self, query):
        pattern = "<passthrough.*?\/>"
        re_passthrough = re.compile(pattern)
	query = re_passthrough.sub('',query)
	return query

    def _getTagValue(self, query, tagname):
        pattern = "<passthrough[^>]*"+tagname+"=\"(?P<key>[^\"]*)\"\/>"
        re_match = re.compile(pattern)
        m = re_match.search(query)
        if not m:
            return query, None

        query = re_match.sub('',query)
        value = m.group('key')
        value = re.sub(' ','',value)
        data = self._load_json('{"key": %s}' % value)

        return query, data["key"]

    def _getPhraseAlignment(self, query):
        return self._getTagValue(query, 'phrase_alignment')
   
    def _getWordAlignment(self, query):
        return self._getTagValue(query, 'word_alignment')

    def _removeXMLTags(self, query):
        pattern = "<[^>]*?>"
        re_tags = re.compile(pattern)
        query = re_tags.sub('',query)
        return query

    def _dump_json(self, data):
        if self.pretty:
            return json.dumps(data, indent=2) + "\n"
        return json.dumps(data) + "\n"

    def _load_json(self, string):
        return json.loads(string)

    @cherrypy.expose
    def translate(self, **kwargs):
        response = cherrypy.response
        response.headers['Content-Type'] = 'application/json'

        errors = self._check_params_translate(kwargs)
        if errors:
            cherrypy.response.status = 400
            return self._dump_json(errors)
        self.log("The server is working on: %s" %repr(kwargs["q"]))

        if "segid" in kwargs :
                segid = kwargs["segid"]
        else:
                segid = "0000"
        self.log("The server is working on segid: %s" %repr(segid))

	if self.verbose > 0:
            self.log("Request before preprocessing: %s" %repr(kwargs["q"]))
        q = self._prepro(self.filter.filter(kwargs["q"]))
        if self.verbose > 0:
            self.log("Request after preprocessing: %s" %repr(q))
        translation = ""
        if q.strip():
            result_queue = Queue.Queue()

            key = self._get_engine_key(segid)
            self.log("KEY: %s" %repr(key))
            self.queue_translate[key].put((result_queue, segid, q))

            try:
                if self.timeout and self.timeout > 0:
                    name, translation = result_queue.get(timeout=self.timeout)
                else:
                    name, translation = result_queue.get()
                self.log("Engine name: %s" %name)
            except Queue.Empty:
                return self._timeout_error(q, 'translation')

        if self.verbose > 0:
            self.log("Translation before sentence-level confidence estimation: %s" %translation)
            self.log("Source before sentence-level confidence estimation: %s" %q)

        sentenceConfidence = None
        if self.sentence_confidence_enabled == 1:
            if self.verbose > 0:
               self.log("Translation before sentence-level confidence estimation: %s" %translation)
               self.log("Source before sentence-level confidence estimation: %s" %q)
            sentenceConfidence = self._get_sentence_confidence("ID", q, translation)
            if self.verbose > 0:
               self.log("Sentence Confidence: %s" %sentenceConfidence)
               self.log("Translation after postprocessing: %s" %translation)

        if self.verbose > 0:
            self.log("Translation before postprocessing: %s" %translation)
        translation = self._postpro(translation)
        if self.verbose > 0:
            self.log("Translation after postprocessing: %s" %translation)

	translation, phraseAlignment = self._getPhraseAlignment(translation)
        if self.verbose > 1:
            self.log("Phrase alignment: %s" %str(phraseAlignment))
            self.log("Translation after removing phrase-alignment: %s" %translation)

        translation, wordAlignment = self._getWordAlignment(translation)
        if self.verbose > 1:
            self.log("Word alignment: %s" %str(wordAlignment))
            self.log("Translation after removing word-alignment: %s" %translation)

        translation = self._getOnlyTranslation(translation)
        if self.verbose > 1:
            self.log("Translation after removing additional info: %s" %translation)

	translationDict = {}
	if translation:
		if (re.match("_NOSUGGESTION_", name)): translationDict["translatedText"] = " "
		else: translationDict["translatedText"] = translation
	if phraseAlignment:
		if (re.match("_NOSUGGESTION_", name)): translationDict["phraseAlignment"] = ""
		else: translationDict["phraseAlignment"] = phraseAlignment
	if wordAlignment:
		if (re.match("_NOSUGGESTION_", name)): translationDict["wordAlignment"] = ""
		else: translationDict["wordAlignment"] = wordAlignment
        if self.sentence_confidence_enabled == 1:
                self.log("sentence_confidence_enabled: passed")
                if sentenceConfidence:
                        if (re.match("_NOSUGGESTION_", name)): translationDict["sentence_confidence"] = ""
			else: translationDict["sentence_confidence"] = sentenceConfidence
        translationDict["systemName"] = name
        translationDict["segmentID"] = segid

        answerDict = {}
        answerDict["translations"] = [translationDict]

        data = {"data" : answerDict}
#        data = {"data" : {"translations" : [translationDict]}}
        self.log("The server is returning: %s" %self._dump_json(data))
        return self._dump_json(data)

    @cherrypy.expose
    def update(self, **kwargs):
        response = cherrypy.response
        response.headers['Content-Type'] = 'application/json'

        errors = self._check_params_update(kwargs)
        if errors:
            cherrypy.response.status = 400
            return self._dump_json(errors)

	source = kwargs["segment"]
	target = kwargs["translation"]
        self.log("The server is updating, segment: %s" %repr(source))
        self.log("The server is updating, translation: %s" %repr(target))

        if "segid" in kwargs :
                segid = kwargs["segid"]
        else:
                segid = "0000"
        self.log("The server is working on segid: %s" %repr(segid))

	key = self._get_updater_key(segid)
        if self.updater[key] == None:
	        answerDict = {}
        	answerDict["code"] = "0"
       		answerDict["string"] = "OK, but this engine/updater combination does not manage user feedback"
                answerDict["systemName"] = key
                answerDict["segmentID"] = segid

        	data = {"data" : answerDict}
        	self.log("The server is returning: %s" %self._dump_json(data))
        	return self._dump_json(data)


        source = self._updater_source_prepro(self.filter.filter(source))
 	source=self._removeXMLTags(source)
 	source=source.encode("utf-8")
        target = self._updater_target_prepro(self.filter.filter(target))
 	target=self._removeXMLTags(target)
 	target=target.encode("utf-8")

        self.log("The server is updating, after preprocessing segment: %s" %repr(source))
        self.log("The server is updating, after preprocessing translation: %s" %repr(target))

        if "extra" in kwargs :
                extra = kwargs["extra"]
                self.log("The server is working on update, extra: %s" %repr(extra))

        annotation = self.updater[key].update(source=source, target=target)
        self.log("The server created this annotation: %s from the current segment and translation" % annotation)

        annotation=annotation.decode("utf-8")

        q = annotation
        if self.verbose > 0:
            self.log("Request Dummy_Input: %s" %repr(q))
        translation = ""
        result_queue = Queue.Queue()

        key = self._get_engine_key(segid)
        self.queue_translate[key].put((result_queue, segid, q))

        try:
	    if self.timeout and self.timeout > 0:
               name, translation = result_queue.get(timeout=self.timeout)
            else:
               name, translation = result_queue.get()
        except Queue.Empty:
            return self._timeout_error(q, 'dummy_translation')
        if self.verbose > 0:
            self.log("Request after translation of Dummy_Input (NOT USED): %s" %repr(translation))

        answerDict = {}
        answerDict["code"] = "0"
        answerDict["string"] = "OK"
        answerDict["systemName"] = name
        answerDict["segmentID"] = segid

        data = {"data" : answerDict}
        self.log("The server is returning: %s" %self._dump_json(data))
        return self._dump_json(data)


    @cherrypy.expose
    def reset(self, **kwargs):
        response = cherrypy.response
        response.headers['Content-Type'] = 'application/json'

        errors = self._check_params_reset(kwargs)
        if errors:
            cherrypy.response.status = 400
            return self._dump_json(errors)

#We assume that the string for resetting is the same for all MT engines, and it is created by any updater

        if "segid" in kwargs :
                segid = kwargs["segid"]
        else:
                segid = "0000"
        self.log("The server is working on segid: %s" %repr(segid))

        k = self._get_updater_key(segid)
        if updater_config[k] == None:
                answerDict = {}
                answerDict["code"] = "0"
                answerDict["string"] = "OK, but this engine/updater combination does not manage this request"
                answerDict["systemName"] = k
                answerDict["segmentID"] = segid

                data = {"data" : answerDict}
                self.log("The server is returning: %s" %self._dump_json(data))
                return self._dump_json(data)

	annotation = self.updater[k].reset()
        if self.verbose > 0:
            self.log("The server created this annotation: %s" % annotation)

        q = annotation
        if self.verbose > 0:
            self.log("Request Dummy_Input: %s" %repr(q))
        for k in moses.keys():
            translation = ""
            result_queue = Queue.Queue()
            self.queue_translate[k].put((result_queue, segid, q))
            try:
                if self.timeout and self.timeout > 0:
                    name, translation = result_queue.get(timeout=self.timeout)
                else:
                    name, translation = result_queue.get()
            except Queue.Empty:
                return self._timeout_error(q, 'dummy_translation')
            if self.verbose > 0:
                self.log("Request after translation of Dummy_Input (NOT USED): %s" %repr(translation))

        answerDict = {}
        answerDict["code"] = "0"
        answerDict["string"] = "OK"

        data = {"data" : answerDict}
        self.log("The server is returning: %s" %self._dump_json(data))
        return self._dump_json(data)


    def log(self, message):
        logger = logging.getLogger('translation_log')
        logger.info(message)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-ip', help='server ip to bind to, default: localhost', default="127.0.0.1")
    parser.add_argument('-port', action='store', help='server port to bind to, default: 8080', type=int, default=8080)
    parser.add_argument('-nthreads', help='number of server threads, default: 8', type=int, default=8)
    parser.add_argument('-moses', dest="moses_path", action='store', help='path to moses executable', default="/home/buck/src/mosesdecoder/moses-cmd/src/moses")
    parser.add_argument('-moses1', dest="moses1_path", action='store', help='path to an other moses executable', default="")
    parser.add_argument('-moses2', dest="moses2_path", action='store', help='path to an other moses executable', default="")
    parser.add_argument('-moses3', dest="moses3_path", action='store', help='path to an other moses executable', default="")
    parser.add_argument('-options', dest="moses_options", action='store', help='moses options, including .ini -async-output -print-id', default="-f phrase-model/moses.ini -v 0 -threads 2 -async-output -print-id")
    parser.add_argument('-options1', dest="moses1_options", action='store', help='options for the additional moses engine, including .ini -async-output -print-id', default="")
    parser.add_argument('-options2', dest="moses2_options", action='store', help='options for the additional moses engine, including .ini -async-output -print-id', default="")
    parser.add_argument('-options3', dest="moses3_options", action='store', help='options for the additional moses engine, including .ini -async-output -print-id', default="")
    parser.add_argument('-prepro', nargs="+", help='complete call to preprocessing script including arguments')
    parser.add_argument('-updater_source_prepro', nargs="+", help='complete call to preprocessing script including arguments for the source text (used for handling update reuqest')
    parser.add_argument('-updater_target_prepro', nargs="+", help='complete call to preprocessing script including arguments for the target text (used for handling update reuqest')
    parser.add_argument('-postpro', nargs="+", help='complete call to postprocessing script including arguments')
    parser.add_argument('-sentence_confidence', nargs="+", help='complete call to sentence-level confidence estiamtion script including arguments')
    parser.add_argument('-pretty', action='store_true', help='pretty print json')
    parser.add_argument('-slang', help='source language code')
    parser.add_argument('-tlang', help='target language code')
    #parser.add_argument('-log', choices=['DEBUG', 'INFO'], help='logging level, default:DEBUG', default='DEBUG')
    parser.add_argument('-logprefix', help='logfile prefix, default: write to stderr')
    parser.add_argument('-timeout', help='timeout for call to translation engine, default: unlimited', type=int)
    parser.add_argument('-verbose', help='verbosity level, default: 0', type=int, default=0)

    #configuration file for the updater
    parser.add_argument('-updater', dest="updater_config", action='store', help='path to the configuration file of the updater', default="")
    parser.add_argument('-updater1', dest="updater1_config", action='store', help='path to the configuration file of the additional updater', default="")
    parser.add_argument('-updater2', dest="updater2_config", action='store', help='path to the configuration file of the additional updater', default="")
    parser.add_argument('-updater3', dest="updater3_config", action='store', help='path to the configuration file of the additional updater', default="")

    #names for the combination of engines and updaters, required if a map is provided
    parser.add_argument('-system-name', dest="system_name", action='store', help='name for the combination of engine and updater', default="")
    parser.add_argument('-system1-name', dest="system1_name", action='store', help='name forthe additional combination of engine and updater', default="")
    parser.add_argument('-system2-name', dest="system2_name", action='store', help='name of the additional combination of engine and updater', default="")
    parser.add_argument('-system3-name', dest="system3_name", action='store', help='name of the additional combination of engine and updater', default="")

    #file for mapping serfver ID to specific engine and updater
    parser.add_argument('-segment2system', dest="segment2system", action='store', help='path to the file containing the map from segment IDs to engine and updater names', default="")

    # persistent threads
    thread_options = parser.add_mutually_exclusive_group()
    thread_options.add_argument('-persist', action='store_true', help='keep pre/postprocessing scripts running')
    thread_options.add_argument('-nopersist', action='store_true', help='don\'t keep pre/postprocessing scripts running')

    args = parser.parse_args(sys.argv[1:])
    persistent_processes = not args.nopersist

    if args.logprefix:
        init_log("%s.trans.log" %args.logprefix)

    moses = {}
    updater_config = {}
    if args.system_name:
        name = args.system_name
    else:
        name = "System_0"
    moses[name] = MosesProc(" ".join((args.moses_path, args.moses_options)),name)
    updater_config[name] = args.updater_config 
    if args.moses1_path != "" and args.updater1_config != "" :
        if args.system1_name:
            name = args.system1_name
        else:
            name = "System_1"
        moses[name] = MosesProc(" ".join((args.moses1_path, args.moses1_options)),name)
        updater_config[name] = args.updater1_config 
    if args.moses2_path != "" and args.updater2_config != "" :
        if args.system2_name:
            name = args.system2_name
        else:
            name = "System_2"
        moses[name] = MosesProc(" ".join((args.moses2_path, args.moses2_options)),name)
        updater_config[name] = args.updater2_config 
    if args.moses3_path != "" and args.updater2_config != "" :
        if args.system3_name:
            name = args.system3_name
        else:
            name = "System_3"
        moses[name] = MosesProc(" ".join((args.moses3_path, args.moses3_options)),name)
        updater_config[name] = args.updater3_config 


    for k in updater_config.keys():
        sys.stderr.write("Active System:|%s|\n" %repr(k))

    assert (len(moses) == len(updater_config)), "number of engines and updaters does not match"

    cherrypy.config.update({'server.request_queue_size' : 1000,
                            'server.socket_port': args.port,
                            'server.thread_pool': args.nthreads,
                            'server.socket_host': args.ip})
    cherrypy.config.update({'error_page.default': json_error})
    cherrypy.config.update({'log.screen': True})
    if args.logprefix:
        cherrypy.config.update({'log.access_file': "%s.access.log" %args.logprefix,
                                'log.error_file': "%s.error.log" %args.logprefix})
    cherrypy.quickstart(Root(moses, updater_config,
                             prepro_cmd = args.prepro, postpro_cmd = args.postpro,
                             sentence_confidence_cmd = args.sentence_confidence,
                             updater_source_prepro_cmd = args.updater_source_prepro,
			     updater_target_prepro_cmd = args.updater_target_prepro,
                             slang = args.slang, tlang = args.tlang,
                             segid_system_map = args.segment2system,
                             pretty = args.pretty,
                             verbose = args.verbose,
                             persistent_processes = persistent_processes))

    for k in moses.keys():
        moses[k].close()
