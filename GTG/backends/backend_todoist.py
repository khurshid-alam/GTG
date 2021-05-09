# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Getting Things GNOME! - a personal organizer for the GNOME desktop
# Copyright (c) 2008-2013 - Lionel Dricot & Bertrand Rousseau
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------

'''
Remember the milk backend
'''

import os
import re
import cgi
import sys
import ast
import time
import uuid
import todoist
import requests
import threading
import datetime
import exceptions
from pprint import pprint
import simplejson as json
import xml.etree.ElementTree as ET
from dateutil.tz import tzutc, tzlocal

#sys.path.append('/usr/share/gtg')
from GTG.backends.genericbackend import GenericBackend
from GTG import _
from GTG.backends.backendsignals import BackendSignals
from GTG.backends.syncengine import SyncEngine, SyncMeme
from GTG.backends.periodicimportbackend import PeriodicImportBackend
from GTG.tools.dates import Date
from GTG.core.task import Task
from GTG.tools.interruptible import interruptible
from GTG.tools.logger import Log


class Backend(PeriodicImportBackend):

    _general_description = {
        GenericBackend.BACKEND_NAME: "backend_todoist",
        GenericBackend.BACKEND_HUMAN_NAME: _("Todoist"),
        GenericBackend.BACKEND_AUTHORS: ["Khurshid Alam"],
        GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
        GenericBackend.BACKEND_DESCRIPTION:
        _("This service synchronizes your tasks with the web service"
          " Todoist:\n\t\thttp://en.todoist.com\n\n"
          "Note: This product uses the Todoist API but is not"
          " endorsed or certified by Todoist"),
    }
    
    _static_parameters = {
        "tags-to-list-dict": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE: ""},
        "period": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT,
            GenericBackend.PARAM_DEFAULT_VALUE: 10, },
        "is-first-run": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL,
            GenericBackend.PARAM_DEFAULT_VALUE: True, },
    }
    
###############################################################################
### Backend standard methods ##################################################
###############################################################################

    def __init__(self, parameters):
        '''
        See GenericBackend for an explanation of this function.
        Loads the saved state of the sync, if any
        '''
        super(Backend, self).__init__(parameters)
        # loading the saved state of the synchronization, if any
        self.sync_engine_path = os.path.join('backends/todoist/',
                                             "sync_engine-" + self.get_id())
        self.sync_engine = self._load_pickled_file(self.sync_engine_path,
                                                   SyncEngine())
        self.api = None
        # reloading the oauth authentication token, if any
        self.token_path = os.path.join('backends/todoist/',
                                       "auth_token-" + self.get_id())
        self.token = self._load_pickled_file(self.token_path, None)
        self.local_dir = os.path.join(os.path.expanduser('~'), '.local/share/gtg')
        self.gtg_xml = os.path.join(self.local_dir, 'gtg_tasks.xml')
        #self.xml_tree = ET.parse(self.gtg_xml)
        #self.rt = self.xml_tree.getroot()
        self.td_cache_path = os.path.join(self.local_dir, 'backends/todoist/todoist_cache')
        self.enqueued_start_get_task = False
        self._this_is_the_first_loop = True
        if (self._parameters["tags-to-list-dict"]):
            s_data = self._parameters["tags-to-list-dict"]
            self.tag_to_project_disc = ast.literal_eval(s_data)
        else:
            self.tag_to_project_disc = None

    def initialize(self):
        """
        See GenericBackend for an explanation of this function.
        """
        super(Backend, self).initialize()
        print (self.token_path)
        self.td_proxy = TDProxy(self.authenticate(),
                  			    self.token,
					            self.td_cache_path)
	    #self.authenticate()

    def save_state(self):
        """
        See GenericBackend for an explanation of this function.
        """
        self._store_pickled_file(self.sync_engine_path, self.sync_engine)

    def authenticate(self):
        """ Try to authenticate by already existing credences or request an authorization """
        self.authenticated = False
        token = self._load_pickled_file(self.token_path, None)
        if not token:
            self._request_authorization()
        else:
            self.apply_credentials(token)

    def apply_credentials(self, token):
        """ Finish authentication or request for an authorization by applying the credentials """
        api = todoist.TodoistAPI(token, cache=self.td_cache_path)
        self.authenticated = True
        
        self._on_successful_authentication()        

    def _request_authorization(self):
        '''
        Calls for a user interaction during authentication
        '''
        BackendSignals().interaction_requested(self.get_id(), _(
            "You need to <b>authorize GTG</b> to access your tasks on <b>Google</b>.\n"
            "<b>Check your browser</b>, and follow the steps there.\n"
            "When you are done, press 'Continue'."),
            BackendSignals().INTERACTION_TEXT,
            "on_authentication_step")

    def on_authentication_step(self, step_type="", code=""):
        """ First time return specification of dialog.
            The second time grab the code and make the second, last
            step of authorization, afterwards apply the new credentials """

        if step_type == "get_ui_dialog_text":
            return _("Code request"), _("Paste the code Google has given you"
                    "here")
        elif step_type == "set_text":
            try:
                self._store_pickled_file(self.token_path, code)
                self.apply_credentials(code)
            except:
                # Show an error to user and end
                self.quit(disable = True)
                BackendSignals().backend_failed(self.get_id(), 
                            BackendSignals.ERRNO_AUTHENTICATION)
                return
            #self.apply_credentials(code)
            # Request periodic import, avoid waiting a long time
            #self.start_get_tasks()

###############################################################################
### TWO WAY SYNC ##############################################################
###############################################################################

    def do_periodic_import(self):
        """
        See PeriodicImportBackend for an explanation of this function.
        """
        # Wait until authentication
        if not self.authenticated:
            return
            
        '''
        td_disc = self.td_proxy.get_td_disc()
        td_task = self.td_proxy.get_td_tasks_dict()[2293384448]
        #tags = ['@home', '@songs', '@test-tag']
        task = self.datastore.get_task("8b676664-7fae-40d5-b7c8-076331beaa23")
        rt = self.xml_tree.getroot()
        t_mod = self.get_param(rt, "8b676664-7fae-40d5-b7c8-076331beaa23", "modified")
        print (type(t_mod), t_mod)
        print (task.get_title())
        #print(int(dt.strftime("%s")))
        print(type(td_task.get_modified()), td_task.get_modified())
        meme = self.sync_engine.get_meme_from_local_id(task.get_id())
        newest = meme.which_is_newest(t_mod,
                                              td_task.get_modified())
                                              
        print (newest)
        
        #tags = gtg_task.get_tags()
        #tags = [tag.get_name() for tag in tags]
        #print (tags)
        #td_task_new = self.td_proxy.create_new_td_task('abdv', tags,
                                                        #self.tag_to_project_disc)
        #pprint (td_task_new)
                                                     

        #pprint (tag_to_project_disc)
        '''
            
        if self._this_is_the_first_loop:
            self._on_successful_authentication()
            
        # we get the old list of synced tasks, and compare with the new tasks
        # set
        stored_td_task_ids = self.sync_engine.get_all_remote()
        current_td_task_ids = [tid for tid in
                                self.td_proxy.get_td_tasks_dict().iterkeys()]
                                
        #print (current_td_task_ids)
        # If it's the very first time the backend is run, it's possible that
        # the user already synced his tasks in some way (but we don't know
        # that). Therefore, we attempt to induce those tasks relationships
        # matching the titles.
        """
        if self._parameters["is-first-run"]:
            gtg_titles_dic = {}
            for tid in self.datastore.get_all_tasks():
                gtg_task = self.datastore.get_task(tid)
                if not self._gtg_task_is_syncable_per_attached_tags(gtg_task):
                    continue
                gtg_title = gtg_task.get_title()
                if gtg_title in gtg_titles_dic:
                    gtg_titles_dic[gtg_task.get_title()].append(tid)
                else:
                    gtg_titles_dic[gtg_task.get_title()] = [tid]
            for td_task_id in current_td_task_ids:
                td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
                try:
                    tids = gtg_titles_dic[td_task.get_title()]
                    # we remove the tid, so that it can't be linked to two
                    # different td tasks
                    tid = tids.pop()
                    gtg_task = self.datastore.get_task(tid)
                    meme = SyncMeme(gtg_task.get_modified(),
                                    td_task.get_modified(),
                                    "GTG")
                    self.sync_engine.record_relationship(
                        local_id=tid,
                        remote_id=td_task.get_id(),
                        meme=meme)
                except KeyError:
                    pass
            # a first run has been completed successfully
            self._parameters["is-first-run"] = False
            """

        for td_task_id in current_td_task_ids:
            self.cancellation_point()
            # Adding and updating
            self._process_td_task(td_task_id)

        for td_task_id in set(stored_td_task_ids).difference(
                set(current_td_task_ids)):
            self.cancellation_point()
            # Removing the old ones
            if not self.please_quit:
                tid = self.sync_engine.get_local_id(td_task_id)
                self.datastore.request_task_deletion(tid)
                try:
                    self.sync_engine.break_relationship(remote_id=td_task_id)
                    self.save_state()
                except KeyError:
                    pass
                    
                    
    def _on_successful_authentication(self):
        '''
        Saves the token and requests a full flush on first autentication
        '''
        self._this_is_the_first_loop = False
        #self._store_pickled_file(self.token_path,
                                 #self.td_proxy.get_auth_token())
        # we ask the Datastore to flush all the tasks on us
        threading.Timer(10,
                        self.datastore.flush_all_tasks,
                        args=(self.get_id(),)).start()

    @interruptible
    def remove_task(self, tid):
        """
        See GenericBackend for an explanation of this function.
        """
        if not self.td_proxy.is_authenticated():
            return
        self.cancellation_point()
        try:
            td_task_id = self.sync_engine.get_remote_id(tid)
            if td_task_id not in self.td_proxy.get_td_tasks_dict():
                # we might need to refresh our task cache
                self.td_proxy.refresh_td_tasks_dict()
            td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
            td_task.delete()
            Log.debug("removing task %s from TD " % td_task_id)
            self.sync_engine.break_relationship(remote_id=td_task_id)
            #self.td_proxy.update_mod_disc(str(td_task_id), None, rm=True)
        except KeyError:
            pass
            try:
                self.sync_engine.break_relationship(local_id=tid)
                self.save_state()
            except:
                pass
                

                
###############################################################################
### Process tasks #############################################################
###############################################################################

    def _process_td_task(self, td_task_id):
        '''
        Takes a rtm task id and carries out the necessary operations to
        refresh the sync state
        '''
        self.cancellation_point()
        if not self.td_proxy.is_authenticated():
            return
        td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
        is_syncable = self._td_task_is_syncable_per_attached_tags(td_task)
        action, tid = self.sync_engine.analyze_remote_id(
            td_task_id,
            self.datastore.has_task,
            self.td_proxy.has_td_task,
            is_syncable)
        #Log.debug("GTG<-TD set task (%s, %s)" % (action, td_task.get_title()))
        
        if action is None:
            return

        if action == SyncEngine.ADD:
            if td_task.get_status() != Task.STA_ACTIVE:
                # OPTIMIZATION:
                # we don't sync tasks that have already been closed before we
                # even saw them
                return
            tid = str(uuid.uuid4())
            task = self.datastore.task_factory(tid)
            self._populate_task(task, td_task)
            meme = SyncMeme(task.get_modified(),
                            td_task.get_modified(),
                            "TD")
            self.sync_engine.record_relationship(
                local_id=tid,
                remote_id=td_task_id,
                meme=meme)
            Log.debug("GTG<-TD adding task (%s, %s)" %(action, td_task.get_title()))
            self.datastore.push_task(task)

        elif action == SyncEngine.UPDATE:
            task = self.datastore.get_task(tid)
            with self.datastore.get_backend_mutex():
                xml_tree = ET.parse(self.gtg_xml)
                rt = xml_tree.getroot()
                local_modified = self.get_param(rt, task.get_id(), "modified")
                if not local_modified:
                    #local_modified = task.get_modified()
                    local_modified = datetime.datetime.strptime("2011-11-11T18:43:37", 
                                                                "%Y-%m-%dT%H:%M:%S")
                #Log.debug("GTG_modified: %s, TD_modified: %s" % (task.get_modified(), 
                                                                #td_task.get_modified()))
                meme = self.sync_engine.get_meme_from_remote_id(td_task_id)
                newest = meme.which_is_newest(local_modified,
                                              td_task.get_modified())
                #print ("newest is {}, {}".format(newest, task.get_title()))                              
                if newest == "remote":
                    Log.debug("GTG<-TD updating task (%s, %s)" %(action, td_task.get_title()))
                    self._populate_task(task, td_task)
                    meme.set_remote_last_modified(td_task.get_modified())
                    meme.set_local_last_modified(task.get_modified())
                elif newest == "local":
                    Log.debug("GTG->TD updating td_task (%s, %s)" % (action, task.get_title()))
                    try:
                        self._populate_td_task(task, td_task)
                    except:
                        raise
                    meme.set_remote_last_modified(td_task.get_modified())
                    meme.set_local_last_modified(local_modified)    
                else:
                    # we skip saving the state
                    return

        elif action == SyncEngine.REMOVE:
            try:
                Log.debug("GTG<-TD removing task (%s, %s)" %(action, td_task.get_title()))
                td_task.delete()
                self.sync_engine.break_relationship(remote_id=td_task_id)
            except KeyError:
                pass

        elif action == SyncEngine.LOST_SYNCABILITY:
            self._exec_lost_syncability(tid, td_task)

        self.save_state()
        

###############################################################################
### Helper methods ############################################################
###############################################################################
    @interruptible
    def set_task(self, task):
        """
        See GenericBackend for an explanation of this function.
        """
        if not self.td_proxy.is_authenticated():
            return
        self.cancellation_point()
        tid = task.get_id()
        is_syncable = self._gtg_task_is_syncable_per_attached_tags(task)
        action, td_task_id = self.sync_engine.analyze_local_id(
            tid,
            self.datastore.has_task,
            self.td_proxy.has_td_task,
            is_syncable)
        #Log.debug("GTG->TD set task (%s, %s)" % (action, task.get_title()))

        if action is None:
            return

        if action == SyncEngine.ADD:
            Log.debug("GTG->TD adding td_task (%s, %s)" % (action, task.get_title()))
            if task.get_status() != Task.STA_ACTIVE:
                # OPTIMIZATION:
                # we don't sync tasks that have already been closed before we
                # even synced them once
                return
            try:
                tags = task.get_tags()
                tags = [tag.get_name() for tag in tags]
                td_task = self.td_proxy.create_new_td_task(task.get_title(),
                                                            tags,
                                                            self.tag_to_project_disc)
                self._populate_td_task(task, td_task)
            except:
                #td_task.delete()
                raise
            meme = SyncMeme(task.get_modified(),
                            td_task.get_modified(),
                            "GTG")
            self.sync_engine.record_relationship(
                local_id=tid, remote_id=td_task.get_id(), meme=meme)

        elif action == SyncEngine.UPDATE:
            try:
                td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
            except KeyError:
                # in this case, we don't have yet the task in our local cache
                # of what's on the rtm website
                self.td_proxy.refresh_td_tasks_dict()
                td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
            with self.datastore.get_backend_mutex():
                try:
                    xml_tree = ET.parse(self.gtg_xml)
                    rt = xml_tree.getroot()
                    local_modified = self.get_param(rt, task.get_id(), "modified")
                    if not local_modified:
                        local_modified = task.get_modified()
                except:
                    local_modified = task.get_modified()                    
                meme = self.sync_engine.get_meme_from_local_id(task.get_id())
                newest = meme.which_is_newest(local_modified,
                                              td_task.get_modified())
                                              
                #Log.debug("GTG_modified: %s, TD_modified: %s" % (task.get_modified(), 
                                                                #td_task.get_modified()))
                if newest == "local":
                    Log.debug("GTG->TD updating td_task (%s, %s)" % (action, task.get_title()))
                    try:
                        self._populate_td_task(task, td_task)
                    except:
                        raise
                    meme.set_remote_last_modified(td_task.get_modified())
                    meme.set_local_last_modified(local_modified)
                else:
                    # we skip saving the state
                    return

        elif action == SyncEngine.REMOVE:
            self.datastore.request_task_deletion(tid)
            try:
                self.sync_engine.break_relationship(local_id=tid)
            except KeyError:
                pass

        elif action == SyncEngine.LOST_SYNCABILITY:
            try:
                td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
            except KeyError:
                # in this case, we don't have yet the task in our local cache
                # of what's on the rtm website
                self.td_proxy.refresh_td_tasks_dict()
                td_task = self.td_proxy.get_td_tasks_dict()[td_task_id]
            self._exec_lost_syncability(tid, td_task)
        
        #Should it be under lost loop....?
        self.save_state()

    def _exec_lost_syncability(self, tid, td_task):
        '''
        Executed when a relationship between tasks loses its syncability
        property. See SyncEngine for an explanation of that.

        @param tid: a GTG task tid
        @param note: a RTM task
        '''
        self.cancellation_point()
        meme = self.sync_engine.get_meme_from_local_id(tid)
        # First of all, the relationship is lost
        self.sync_engine.break_relationship(local_id=tid)
        if meme.get_origin() == "GTG":
            td_task.delete()
        else:
            self.datastore.request_task_deletion(tid)
        self.save_state()

    def _populate_task(self, task, td_task):
        '''
        Copies the content of a tdTask in a Task
        '''
        task.set_title(td_task.get_title())
        #task.set_text(td_task.get_text())
        task.set_due_date(td_task.get_due_date())
        status = td_task.get_status()
        #Log.debug("GTG<-TD completing gtg_task (%s)" % (task.get_title()))
        if task.get_status() != status:
            task.set_status(td_task.get_status())
        # tags
        tags = set(['@%s' % tag for tag in td_task.get_tags()])
        gtg_tags_lower = set([t.get_name().lower() for t in task.get_tags()])
        # tags to remove
        for tag in gtg_tags_lower.difference(tags):
            task.remove_tag(tag)
        # tags to add
        for tag in tags.difference(gtg_tags_lower):
            gtg_all_tags = self.datastore.get_all_tags()
            matching_tags = filter(lambda t: t.lower() == tag, gtg_all_tags)
            if len(matching_tags) != 0:
                tag = matching_tags[0]
            task.add_tag(tag)
        #Set additional task attributes...useful future reference
        task.set_attribute("project_id", td_task.td_project)
        task.set_attribute("remote_id", td_task.get_id())

    def _populate_td_task(self, task, td_task):
        '''
        Copies the content of a Task into a TDTask

        @param task: a GTG Task
        @param td_task: an tdTask
        @param transaction_ids: a list to fill with transaction ids
        '''
        # Get methods of an td_task are fast, set are slow: therefore,
        # we try to use set as rarely as possible

        # first thing: the status. This way, if we are syncing a completed
        # task it doesn't linger for ten seconds in the td Inbox
        status = task.get_status()
        if td_task.get_status() != status:
            self.__call_or_retry(td_task.set_status, status)
        title = task.get_title()
        if td_task.get_title() != title:
            self.__call_or_retry(td_task.set_title, title)
        #text = task.get_excerpt(strip_tags=True, strip_subtasks=True)
        #if td_task.get_text() != text:
            #self.__call_or_retry(td_task.set_text, text)
        tags = task.get_tags_name()
        td_task_tags = []
        for tag in td_task.get_tags():
            if tag[0] != '@':
                tag = '@' + tag
            td_task_tags.append(tag)
        # td tags are lowercase only
        if td_task_tags != [t.lower() for t in tags]:
            self.__call_or_retry(td_task.set_tags, tags)
        due_date = task.get_due_date()
        #Log.debug("Gtg due date: %s", task.get_due_date())
        #Log.debug("Td_task due date: %s", td_task.get_due_date())
        if td_task.get_due_date() != due_date:
            self.__call_or_retry(td_task.set_due_date, due_date)
            #Log.debug("Td_task due date: %s", td_task.get_due_date())
         
    def __call_or_retry(self, fun, *args):
        '''
        This function cannot stand the call "fun" to fail, so it retries
        three times before giving up.
        '''
        MAX_ATTEMPTS = 3
        for i in xrange(MAX_ATTEMPTS):
            try:
                return fun(*args)
            except:
                if i >= MAX_ATTEMPTS:
                    raise

    def _td_task_is_syncable_per_attached_tags(self, td_task):
        '''
        Helper function which checks if the given task satisfies the filtering
        imposed by the tags attached to the backend.
        That means, if a user wants a backend to sync only tasks tagged @works,
        this function should be used to check if that is verified.

        @returns bool: True if the task should be synced
        '''
        attached_tags = self.get_attached_tags()
        if GenericBackend.ALLTASKS_TAG in attached_tags:
            return True
        for tag in td_task.get_tags():
            if "@" + tag in attached_tags:
                return True
        return False
        
        
    #Hack to get local modified time as task.get_modified always returns
    #current datetime
    
    def item_exists(self, rt, task_id):
        exp =  ".//*[@id='{}']".format(task_id)
        if rt.find(exp) is not None:
            return True
        else:
            return False
            
    def get_param(self, rt, task_id, param):
        exp =  ".//*[@id='{}']".format(task_id)
        if rt.find(exp) is not None:
            for task in rt.findall(exp):
                output = task.find(param).text
                output = datetime.datetime.strptime(output, "%Y-%m-%dT%H:%M:%S")
                if output:
                    return output
                else:
                    return None
        else:
            return None         



###############################################################################
### Convert dict to object recursively ########################################
###############################################################################
class Struct(object):
    """Comment removed"""
    def __init__(self, data):
        for name, value in data.iteritems():
            setattr(self, name, self._wrap(value))

    def _wrap(self, value):
        if isinstance(value, (tuple, list, set, frozenset)): 
            return type(value)([self._wrap(v) for v in value])
        else:
            return Struct(value) if isinstance(value, dict) else value



class TDProxy(object):
    '''
    The purpose of this class is producing an updated list of RTMTasks.
    To do that, it handles:
        - authentication to RTM
        - keeping the list fresh
        - downloading the list
    '''

    PUBLIC_KEY = "2a440fdfe9d890c343c25a91afd84c7e"
    PRIVATE_KEY = "ca078fee48d0bbfa"

    def __init__(self,
                auth_confirm_fun,
                token=None,
                td_cache=None):
        self.auth_confirm = auth_confirm_fun
        self.token = token
        self.td_cache = td_cache 
        self.authenticated = threading.Event()
        self.login_event = threading.Event()
        self.is_not_refreshing = threading.Event()
        self.is_not_refreshing.set()

    ##########################################################################
    ### AUTHENTICATION #######################################################
    ##########################################################################

    def start_authentication(self):
        '''
        Launches the authentication process
        '''
        initialize_thread = threading.Thread(target=self._authenticate)
        initialize_thread.setDaemon(True)
        initialize_thread.start()

    def is_authenticated(self):
        '''
        Returns true if we've autheticated to RTM
        '''
        return self.authenticated.isSet()

    def wait_for_authentication(self):
        '''
        Inhibits the thread until authentication occours
        '''
        self.authenticated.wait()

    def get_auth_token(self):
        '''
        Returns the oauth token, or none
        '''
        try:
            return self.token
        except:
            return None

    def _authenticate(self):
        '''
        authentication main function
        '''
        self.authenticated.clear()
        while not self.authenticated.isSet():
            if not self.token:
                self.auth_confirm()
                self.td = todoist.TodoistAPI(self.token, cache=self.td_cache)
            try:
                if self._login():
                    self.authenticated.set()
            except exceptions.IOError:
                BackendSignals().backend_failed(self.get_id(),
                                                BackendSignals.ERRNO_NETWORK)

    def _login(self):
        '''
        Tries to establish a connection to rtm with a token got from the
        authentication process
        '''
        try:
            self.td = todoist.TodoistAPI(self.token, cache=self.td_cache)
            return True
        except:
            Log.error("RTM ERROR")
        return False

    ##########################################################################
    ### TD TASKS HANDLING ###################################################
    ##########################################################################

    def get_task_by_id(self, item_id):
        Log.debug('refreshing todoist')
        return (self.td.items.get_by_id(item_id))

    def get_api(self):
        return (todoist.TodoistAPI(self.token, cache=self.td_cache))
        
    def save_to_json(self, data, file_path):
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
            
    def load_from_json(self, file_path):
        with open(file_path) as data_file:
            data_loaded = json.load(data_file)
        return data_loaded
        
    def make_mod_disc(self):
        p = self.td_cache.split('todoist_cache')[0]
        td_cache_file = p + 'todoist_cache' + self.token + '.json'
        td_modified_file = p + 'todoist_mod' + self.token + '.json'
        td_disc = self.load_from_json(td_cache_file)
        if not os.path.exists(td_modified_file):        
            m = {}
            td_disc = self.load_from_json(td_cache_file)
            td_mod_stamp = os.path.getmtime(td_cache_file)
            for td_task_disc in td_disc['items']:
                m[str(td_task_disc['id'])] = td_mod_stamp    
            self.save_to_json(m, td_modified_file)
            return m
            
    def get_mod_disc(self):
        p = self.td_cache.split('todoist_cache')[0]
        td_modified_file = p + 'todoist_mod' + self.token + '.json'
        if not os.path.exists(td_modified_file):
            mod_disc = self.make_mod_disc()
            return (mod_disc, td_modified_file)
        else:
            mod_disc = self.load_from_json(td_modified_file)
            return (mod_disc, td_modified_file)
            
    def update_mod_disc(self, td_task_id, td_mod_stamp, rm=False):
        if rm:
            mod_disc, td_modified_file = self.get_mod_disc()
            Log.debug("Removing item from mod_disc")
            if td_task_id in mod_disc: 
                del mod_disc[td_task_id]
            self.save_to_json(mod_disc, td_modified_file)
        else:
            Log.debug("Updating mod_disc......")        
            mod_disc, td_modified_file = self.get_mod_disc()
            mod_disc[str(td_task_id)] = td_mod_stamp
            self.save_to_json(mod_disc, td_modified_file)
                
        
    def get_td_disc(self):
        p = self.td_cache.split('todoist_cache')[0]
        td_cache_file = p + 'todoist_cache' + self.token + '.json'
        with open(td_cache_file) as jsondata:
            td_disc = json.load(jsondata)
        return td_disc
                
        
    def get_td_tasks_dict(self):
        '''
        Returns a dict of RTMtasks. It will start authetication if necessary.
        The dict is kept updated automatically.
        '''
        if not hasattr(self, '_td_task_dict'):
            self.refresh_td_tasks_dict()
        else:
            time_difference = datetime.datetime.now() - \
                self.__td_task_dict_timestamp
            if time_difference.seconds > 60:
                self.refresh_td_tasks_dict()
        return self._td_task_dict.copy()

    def refresh_td_tasks_dict(self):
        '''
        Builds a list of RTMTasks fetched from RTM
        '''
        if not self.is_authenticated():
            self.start_authentication()
            self.wait_for_authentication()

        if not self.is_not_refreshing.isSet():
            # if we're already refreshing, we just wait for that to happen and
            # then we immediately return
            self.is_not_refreshing.wait()
            return
        self.is_not_refreshing.clear()
        Log.debug('refreshing todoist......................................')

        # To understand what this function does, here's a sample output of the
        # plain getLists() from RTM api:
        #    http://www.rememberthemilk.com/services/api/tasks.rtm

        # our purpose is to fill this with "tasks_id: RTMTask" items
        td_tasks_dict = {}
        p = self.td_cache.split('todoist_cache')[0]
        td_cache_file = p + 'todoist_cache' + self.token + '.json'
        td_modified_file = p + 'todoist_mod' + self.token + '.json'
        
        #get modified
        if not os.path.exists(td_cache_file):
            self.td.sync()
            time.sleep(5)
            mod_disc = self.make_mod_disc()
            td_disc = self.load_from_json(td_cache_file)
            remote_items_modified = False

        else:
            #print ("getting old modification time")
            #mod_disc, t = self.get_mod_disc()
                    
            # Now get td_disc
            td_disc_new = self.td.items.sync()
            pprint(td_disc_new)
            td_disc = self.load_from_json(td_cache_file)

            if (len(td_disc_new['items'])) > 0:
                remote_items_modified = True
            else:
                remote_items_modified = False
        
        #get label_name_dict
        td_labels = {}
        for label in td_disc['labels']:
            td_labels[label['id']] = label['name']
            
        #hack: get mod disc
        r = requests.get("https://dl.dropboxusercontent.com/s/t4tcypscgpf9xn7/modified.json", stream=True)
        mod_disc = (ast.literal_eval(r.content))
            	
        for td_task_disc in td_disc['items']:
            td_task_id = str(td_task_disc['id'])
            if td_task_id in mod_disc:
                td_timestamp = int(mod_disc[td_task_id])
                td_mod_final = datetime.datetime.fromtimestamp(td_timestamp)
            else:
                td_mod_final = datetime.datetime.now()               
            label_names = [td_labels[i] for i in td_task_disc['labels']]
            #td_task_disc['label_names'] = label_names
            td_task = Struct(td_task_disc)
            td_tasks_dict[td_task.id] = TDTask(td_task,
                                                td_task.project_id,
                                                td_mod_final,
                                                td_cache_file,
                                                self.td)

        # we're done: we store the dict in this class 
        #and we annotate the time we got it
        self._td_disc = td_disc
        self._td_task_dict = td_tasks_dict
        self.__td_task_dict_timestamp = datetime.datetime.now()
        self.is_not_refreshing.set()
        
    def has_td_task(self, td_task_id):
        '''
        Returns True if we have seen that task id
        '''
        cache_result = td_task_id in self.get_td_tasks_dict()
        return cache_result
        # it may happen that the td_task is on the website but we haven't
        # downloaded it yet. We need to update the local cache.

        # it's a big speed loss. Let's see if we can avoid it.
        # self.refresh_td_tasks_dict()
        # return td_task_id in self.get_td_tasks_dict()
        
    def create_new_td_task(self, title, tags, tag_to_project_disc):
        '''
        Creates a new td task
        '''
        self.td = self.get_api()
        td_disc = self.get_td_disc()
        if tags: #gtg tags found, we must create new td task with it
            tags = [tag[1:].lower() for tag in tags]
            # get label_ids []
            label_ids = []
            td_labels = {}
            for label in td_disc['labels']:
                td_labels[label['name']] = label['id']
                
            #create lebels if it doesn't exist
            for n in tags:
                if n not in td_labels:
                    self.td.labels.add(n)
                    self.td.commit()
                    
            #get new td_labels {}
            self.td.sync()
            td_disc = self.get_td_disc()        
            for label in td_disc['labels']:
                td_labels[label['name']] = label['id']      
            for n in tags:
                label_ids.append(td_labels[n])
            #pprint (label_ids)    
                
            if tag_to_project_disc: #disc provided, we should look for project_id
                for tag in tags:
                    if tag in tag_to_project_disc:
                        project_id = tag_to_project_disc[tag]
                        break                
                if project_id:
                    print (project_id)                     
                    api_call = self.td.items.add(title, project_id=project_id, labels=label_ids)
                else: # But project not found, must be new tag in gtg, so use Inbox
                    api_call = self.td.items.add(title, '', labels=label_ids)
            else:
                api_call = self.td.items.add(title, '',labels=label_ids)    
        else: #no tags, we create a simple task in Inbox
            api_call = self.td.items.add(title)
        
        #print (api_call)
        result = self.td.commit()
        if len(result['items']) > 1:            
            task_id = result['items'][1]['id']
            task_obj = Struct(result['items'][1])
        else:
            task_id = result['items'][0]['id']
            task_obj = Struct(result['items'][0])
            
        p = self.td_cache.split('todoist_cache')[0]
        td_cache_file = p + 'todoist_cache' + self.token + '.json'
        td_task_mod = os.path.getmtime(td_cache_file)
        td_mod_final3 = datetime.datetime.fromtimestamp(int(td_task_mod))
        td_mod_final2 = td_mod_final3.strftime("%Y-%m-%dT%H:%M:%S")
        td_mod_final = datetime.datetime.strptime(td_mod_final2, "%Y-%m-%dT%H:%M:%S")
        
        td_task = TDTask(task_obj,
                        task_obj.project_id,
                        td_mod_final,
                        td_cache_file,
                        self.td)
        # adding to the dict right away
        if hasattr(self, '_td_task_dict'):
            # if the list hasn't been downloaded yet, we do not create a list,
            # because the fact that the list is created is used to keep track
            # of list updates
            self._td_task_dict[td_task.get_id()] = td_task
        return td_task
				
		

###############################################################################
### TD TASK ##################################################################
###############################################################################
# dictionaries to translate a RTM status into a GTG one (and back)
GTG_TO_TD_STATUS = {Task.STA_ACTIVE: True,
                     Task.STA_DONE: False,
                     Task.STA_DISMISSED: False}

TD_TO_GTG_STATUS = {True: Task.STA_ACTIVE,
                     False: Task.STA_DONE}
class TDTask(object):
    '''
    A proxy object that encapsulates a RTM task, giving an easier API to access
    and modify its attributes.
    This backend already uses a library to interact with RTM, but that is just
     a thin proxy for HTML gets and posts.
    The meaning of all "special words"

    http://www.rememberthemilk.com/services/api/tasks.rtm
    '''

    def __init__(self, td_task, td_project, td_task_mod, td_cache_file, td):
        '''
        sets up the various parameters needed to interact with a task.

        @param task: the task object given by the underlying library
        @param rtm_list: the rtm list the task resides in.
        @param rtm_taskseries: all the tasks are encapsulated in a taskseries
            object. From RTM website::

            A task series is a grouping of tasks generated by a recurrence
            pattern (more specifically, a recurrence pattern of type every – an
            after type recurrence generates a new task series for every
            occurrence). Task series' share common properties such as:
                - Name.
                - Recurrence pattern.
                - Tags.
                - Notes.
                - Priority.
        @param rtm: a handle of the rtm object, to be able to speak with rtm.
                    Authentication should have already been done.
        @param timeline: a "timeline" is a series of operations rtm can undo in
                         bulk. We are free of requesting new timelines as we
                         please, with the obvious drawback of being slower.
        '''
        self.td_task = td_task
        self.td_project = td_project
        self.td_task_mod = td_task_mod
        self.td_cache_file = td_cache_file
        self.td = td
        
    def get_td_disc(self):
        with open(self.td_cache_file) as jsondata:
            td_disc = json.load(jsondata)
        return td_disc

    def get_id(self):
        '''Return the task id. The taskseries id is *different*'''
        return self.td_task.id

    def get_title(self):
        '''Returns the title of the task, if any'''
        return self.td_task.content
        '''
        g = re.search("(?P<url>https?://[^\s]+)", task_c)
        if g:
            task_url = g.group()
            regex = re.compile(".*?\((.*?)\)")
            task_content = re.findall(regex, task_c)[0]
            return task_content
        else: 
            task_c'''

    def set_title(self, title):
        '''Sets the task title'''
        title = cgi.escape(title)
        self.td.items.update(self.td_task.id,
                                content=title)
        result = self.td.commit()

    def get_modified(self):
        return self.td_task_mod
        
    def get_status(self):
        '''Returns the task status, in GTG terminology'''
        return TD_TO_GTG_STATUS[self.td_task.checked == 0]

    def set_status(self, gtg_status):
        '''Sets the task status, in GTG terminology'''
        status = GTG_TO_TD_STATUS[gtg_status]
        if status is True:
            result = self.td.items.uncomplete(self.td_task.id)
        else:
            result = self.td.items.complete(self.td_task.id)
        self.td.commit()
        
    def get_tags(self):
        '''Returns the task tags'''
        td_disc = self.get_td_disc()
        td_labels = {}
        for label in td_disc['labels']:
            td_labels[label['id']] = label['name']
                    
        if len(self.td_task.labels) > 0:
            l = [td_labels[i] for i in self.td_task.labels]
            return l
        else:
            return []
            
    def set_tags(self, tags):
        td_disc = self.get_td_disc()
        
        if tags: #gtg tags found, we must create new td task with it
            tags = [tag[1:].lower() for tag in tags]
            # get label_ids []
            label_ids = []
            td_labels = {}
            for label in td_disc['labels']:
                td_labels[label['name']] = label['id']
                
            #create lebels if it doesn't exist
            for n in tags:
                if n not in td_labels:
                    self.td.labels.add(n)
                    self.td.commit()
                    
            #get new td_labels {}
            self.td.sync()
            td_disc = self.get_td_disc()        
            for label in td_disc['labels']:
                td_labels[label['name']] = label['id']      
            for n in tags:
                label_ids.append(td_labels[n])
                
            self.td.items.update(self.td_task.id, labels=label_ids)
            self.td.commit()
            
    def get_text(self, td_disc):
        '''
        Gets the content of RTM notes, aggregated in a single string
        '''
        note_disc = {}
        #to-do
        
            
        
    def get_due_date(self):
        '''
        Gets the task due date
        '''
        if hasattr(self.td_task, 'due'):
            due = self.td_task.due

            if due == "" or due is None:
                return Date.no_date()
            else:
                due = due.date
                date = self.__time_td_to_datetime(due).date()
                return Date(date)
        else:
            return Date.no_date()
        
    def set_due_date(self, due):
        if due is None:
            self.td.items.update(self.td_task.id, due=None)
        else:
            due_c = {"date": self.__time_date_to_td(due)}
            self.td.items.update(self.td_task.id, due=due_c)
        self.td.commit()
        
    def delete(self):
        self.td.items.delete(self.td_task.id)
        self.td.commit()
        
    # TD speaks utc, and accepts utc if the "parse" option is set.
    def __tz_utc_to_local(self, dt):
        dt = dt.replace(tzinfo=tzutc())
        dt = dt.astimezone(tzlocal())
        return dt.replace(tzinfo=None)

    def __tz_local_to_utc(self, dt):
        dt = dt.replace(tzinfo=tzlocal())
        dt = dt.astimezone(tzutc())
        return dt.replace(tzinfo=None)

    def __time_td_to_datetime(self, string):
        #string = string.split(' +0000')[0]
        #print (string)
        if "T" in string:
            dt = datetime.datetime.strptime(string,
                                        "%Y-%m-%dT%H:%M:%SZ")
        else:
            dt = datetime.datetime.strptime(string,
                                        "%Y-%m-%d")
        #return self.__tz_utc_to_local(dt)
        return dt

    def __time_td_to_date(self, string):
        #string = string.split(' +0000')[0]
        dt = datetime.datetime.strptime(string,
                                        "%a %d %b %Y %H:%M:%S")
        #return self.__tz_utc_to_local(dt)
        return dt

    def __time_datetime_to_td(self, timeobject):
        if timeobject is None:
            return ""
        #timeobject = self.__tz_local_to_utc(timeobject)
        return timeobject.strftime("%Y-%m-%dT%H:%M:%S")

    def __time_date_to_td(self, timeobject):
        if timeobject is None:
            return None
        # WARNING: no timezone? seems to break the symmetry.
        return timeobject.strftime("%Y-%m-%d")

    def __str__(self):
        return "Task %s (%s)" % (self.get_title(), self.get_id())
	
    

