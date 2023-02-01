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
import gitlab
import requests
import threading
import datetime
import exceptions
import dateutil.parser
from pprint import pprint
import simplejson as json
#import xml.etree.ElementTree as ET
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
from GTG.core import CoreConfig
from GTG.tools import cleanxml, taskxml


class Backend(PeriodicImportBackend):

	_general_description = {
		GenericBackend.BACKEND_NAME: "backend_gitlab",
		GenericBackend.BACKEND_HUMAN_NAME: _("Gitlab Issues"),
		GenericBackend.BACKEND_AUTHORS: ["Khurshid Alam"],
		GenericBackend.BACKEND_TYPE: GenericBackend.TYPE_READWRITE,
		GenericBackend.BACKEND_DESCRIPTION:
		_("This service synchronizes your tasks with the web service"
		  " Gitlab issue tracker"
		  "Note: This product uses the Gitlab API but is not"
		  " endorsed or certified by Gitlab"),
	}
    
	_static_parameters = {
		"period": {
			GenericBackend.PARAM_TYPE: GenericBackend.TYPE_INT,
			GenericBackend.PARAM_DEFAULT_VALUE: 5, },
		"username": {
			GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
			GenericBackend.PARAM_DEFAULT_VALUE: 'yourproject/GTG', },
		"password": {
			GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
			GenericBackend.PARAM_DEFAULT_VALUE: 'MQgfWbNsKUY8WTQhhWMN', },
		"service-url": {
			GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
			GenericBackend.PARAM_DEFAULT_VALUE: 'https://gitlab.com', },
		"is-first-run": {
			GenericBackend.PARAM_TYPE: GenericBackend.TYPE_BOOL,
			GenericBackend.PARAM_DEFAULT_VALUE: True, },
        "path": {
            GenericBackend.PARAM_TYPE: GenericBackend.TYPE_STRING,
            GenericBackend.PARAM_DEFAULT_VALUE: "gtg_tasks.xml", },
	}
    
###############################################################################
# Backend standard methods ####################################################
###############################################################################
	def __init__(self, parameters):
		"""
		See GenericBackend for an explanation of this function.
		Re-loads the saved state of the synchronization
		"""
		super(Backend, self).__init__(parameters)
		# loading the saved state of the synchronization, if any
		self.sync_engine_path = os.path.join('backends/gitlab/',
				                             "sync_engine-" + self.get_id())
		self.sync_engine = self._load_pickled_file(self.sync_engine_path,
				                                   SyncEngine())
		self._mutex = threading.Lock()

	def save_state(self):
		"""Saves the state of the synchronization"""
		self._store_pickled_file(self.sync_engine_path, self.sync_engine)

	def get_path(self):
		"""
		Return the current path to XML

		Path can be relative to projects.xml
		"""
		path = self._parameters["path"]
		if os.sep not in path:
			# Local path
			data_dir = CoreConfig().get_data_dir()
			path = os.path.join(data_dir, path)
		return os.path.abspath(path)

	def initialize(self):
		super(Backend, self).initialize()

		#Authentication
		self._gl = gitlab.Gitlab(self._parameters['service-url'], 
										private_token=self._parameters['password'])
										
		try:
			self.cancellation_point()
			self._project = self._gl.projects.get(self._parameters['username'])
		except KeyError:
			self.quit(disable=True)
			BackendSignals().backend_failed(self.get_id(),
				                            BackendSignals.ERRNO_AUTHENTICATION
				                            )
			return


        #self._cached_gl_issue_urls = {}
        
###############################################################################
# Import tasks ################################################################
###############################################################################

	def do_periodic_import(self):
		"""
		See PeriodicImportBackend for an explanation of this function.
		"""

		#Fetching the issues
		self.cancellation_point()
		self._gl_issues = []
		self._gl_issues = self._project.issues.list(all=True) #Network Call

		# If it's the very first time the backend is run, it's possible that
		# the user already synced his tasks in some way (but we don't know
		# that). Therefore, we attempt to induce those tasks relationships
		# matching the url attribute.
		
		if self._parameters["is-first-run"]:
			with self._mutex:
				for tid in self.datastore.get_all_tasks():
			   		try:
						url = self.sync_engine.get_remote_id(tid)
					except KeyError:
						continue

					task = self.datastore.get_task(tid)
					try:
						url = task.get_attribute("web_url")
					except KeyError:
						continue

					for gl_issue in gl_issues:
						if url == str(gl_issue.web_url):
							todo = gl_issue


					meme = SyncMeme(task.get_modified() or Date.no_date(),
						            self._get_gl_issue_modified(todo) or Date.no_date(),
						            "GTG")
					self.sync_engine.record_relationship(
						local_id=tid,
						remote_id=str(todo.web_url),
						meme=meme)
				# a first run has been completed successfully
				self._parameters["is-first-run"] = False
		

		# Adding and updating
		for gl_issue in self._gl_issues:
			self.cancellation_point()
			self._process_gl_issue(gl_issue)

		# Remove Old Issues
		#stored_gl_issue_web_urls = set(self.sync_engine.get_all_remote())
		#new_gl_issues = set([str(gl_issue.web_url) for gl_issue in gl_issues])
		
	def _process_gl_issue(self, gl_issue):

		is_syncable = self._gl_issue_is_syncable_per_attached_tags(gl_issue)
		has_task = self.datastore.has_task
		has_gl_issue = self._has_gl_issue
		action, tid = self.sync_engine.analyze_remote_id(
										str(gl_issue.web_url),
										has_task,
										has_gl_issue,
										is_syncable)


		if action is None:
			return

		with self.datastore.get_backend_mutex():
			if action == SyncEngine.ADD:
				issue_status = self._get_gl_issue_status (gl_issue)
				if issue_status != Task.STA_ACTIVE:
					# OPTIMIZATION:
					# we don't sync tasks that have already been closed before we
					# even synced them once
					return


				tid = str(uuid.uuid4())
				task = self.datastore.task_factory(tid)
				self._populate_task(task, gl_issue)
				meme = SyncMeme(task.get_modified() or Date.no_date(),
								self._get_gl_issue_modified(gl_issue) or Date.no_date(),
								"GITLAB")
				self.sync_engine.record_relationship(
												local_id=tid,
												remote_id=str(gl_issue.web_url),
												meme=meme)
				self.datastore.push_task(task)

			elif action == SyncEngine.UPDATE:
				task = self.datastore.get_task(tid)
				if task.get_title() == "(no title task)":
					return
				meme = self.sync_engine.get_meme_from_remote_id(str(gl_issue.web_url))
				newest = meme.which_is_newest(meme.get_local_last_modified(),
								              self._get_gl_issue_modified(gl_issue))

				Log.debug("[PI] T: {}\nNW: {}\nTM: {}\nIM: {}\nM_RM: {}\nM_LM: {}\n\n".format(task.get_title(),
												newest, 
												task.get_modified(), 
												self._get_gl_issue_modified(gl_issue),
												meme.get_remote_last_modified(),
												meme.get_local_last_modified()))
				if newest == "remote":
					Log.debug("GTG<-Gitlab updating task (%s, %s)" %(action, task.get_title()))
					self._populate_task(task, gl_issue)
					meme.set_remote_last_modified(self._get_gl_issue_modified(gl_issue))
					meme.set_local_last_modified(task.get_modified())
				else:
					# we skip saving the state
					return

			elif action == SyncEngine.REMOVE:
				Log.debug("GTG->Gitlab removing td_task %s" %(action, str(gl_issue.web_url)))
				self._project.issues.delete(gl_issue.iid)
				try:
					self.sync_engine.break_relationship(remote_id=str(gl_issue.web_url))
				except KeyError:
					pass

			elif action == SyncEngine.LOST_SYNCABILITY:
				self._exec_lost_syncability(tid, gl_issue)

			self.save_state()


	def _populate_task(self, task, gl_issue):
		"""
		Copies the content of a gl_issue in a Task
		"""

		task.set_title(str(gl_issue.title) or '')
		task.set_text(str(gl_issue.description) or '')


		# attributes
		task.set_attribute("web_url", str(gl_issue.web_url))
		task.set_attribute("gl_issue_id", gl_issue.id)
		task.set_attribute("gl_issue_iid", gl_issue.iid)
		remote_modified_dt = self._gl_date_to_datetime(str(gl_issue.updated_at))
		task.set_attribute("gl_issue_modified", remote_modified_dt.strftime("%Y-%m-%dT%H:%M:%S"))

		# Status
		if str(gl_issue.state) == "closed":
			task.set_status(Task.STA_DONE)
		else:
			task.set_status(Task.STA_ACTIVE)


		# Dates
		task.set_due_date(self._get_gl_issue_due_date(gl_issue))
		#task.set_added_date(todo.get_added_date())
		#task.set_closed_date(todo.get_closed_date())
		#task.set_start_date(todo.get_start_date())


		# tags
		tags = set([self._tag_to_gtg(tag) for tag in self._get_gl_issue_tags(gl_issue)])
		gtg_tags = set([t.get_name().lower() for t in task.get_tags()])
		# tags to remove
		for tag in gtg_tags.difference(tags):
			task.remove_tag(tag)
		# tags to add
		for tag in tags.difference(gtg_tags):
			task.add_tag(tag)




###############################################################################
# Process tasks ###############################################################
###############################################################################
	@interruptible
	def remove_task(self, tid):
		"""
		See GenericBackend for an explanation of this function.
		"""
		with self.datastore.get_backend_mutex():
			try:
				url = self.sync_engine.get_remote_id(tid)
				if not url:
					task = self.datastore.get_task(tid)
					url  = task.get_attribute("web_url")
				Log.debug("GTG->TD remove_task %s %s" % (tid, url))
				gl_issue_iid = os.path.basename(url)
				self._project.issues.delete(gl_issue_iid)
					
			except KeyError:
				pass

			try:
				self.sync_engine.break_relationship(local_id=tid)
			except KeyError:
				pass



	@interruptible
	def set_task(self, task):
		"""
		See GenericBackend for an explanation of this function.
		"""
		self.cancellation_point()
		with self.datastore.get_backend_mutex():
			tid = task.get_id()
			has_task = self.datastore.has_task
			has_gl_issue = self._has_gl_issue
			is_syncable = self._gtg_task_is_syncable_per_attached_tags(task)
			action, url = self.sync_engine.analyze_local_id(
														tid,
														has_task,
														has_gl_issue,
														is_syncable)
			#Log.debug("GTG->TD set task (%s, %s)" % (action, task.get_title()))

			if action is None:
				return

			if task.get_title() == "(no title task)":
				return

			if task.get_title() == "My new task":
				return

			if len(task.get_tags()) < 1:
				return

			elif action == SyncEngine.ADD:
				time.sleep(2)
				if not task.get_tags():
					return
				Log.debug("GTG->Gitlab adding td_task (%s, %s)" % (action, task.get_title()))

				gl_issue_new = self._create_gl_issue(task)
				self._populate_gl_issue(task, gl_issue_new)

		   		meme = SyncMeme(task.get_modified() or Date.no_date(),
								self._get_gl_issue_modified(gl_issue_new) or Date.no_date(),
								"GTG")
				self.sync_engine.record_relationship(
												local_id=tid, remote_id=str(gl_issue_new.web_url),
												meme=meme)

				# Refresh gl_issues list to avoid task deletetion in set task when it runs
				# again before next periodic sync and it can't find the new task in previously fetched
				# gl_issues list. Ideally this should be avoided by caching the issue_list
				
				time.sleep(1)
				self._gl_issues = self._project.issues.list() #Network Call


			elif action == SyncEngine.UPDATE:
				time.sleep(2) # We give time for backend_localfile to change the modified date
				gl_issue_iid = task.get_attribute("gl_issue_iid")
				if gl_issue_iid:
					gl_issue = self._project.issues.get(gl_issue_iid)
				else:
					gl_issue_iid = os.path.basename(self.sync_engine.get_remote_id(tid))
					gl_issue = self._project.issues.get(gl_issue_iid)
				meme = self.sync_engine.get_meme_from_local_id(task.get_id())
				task_modified_alt = self._get_task_modified_alt(task.get_id())
				newest = meme.which_is_newest(task_modified_alt,
												self._get_gl_issue_modified(gl_issue) or Date.no_date())

				
				Log.debug("[ST] T: {}\nNW: {}\nTM: {}\nIM: {}\nM_RM: {}\nM_LM: {}\n\n".format(task.get_title(),
												newest, 
												task.get_modified(), 
												self._get_gl_issue_modified(gl_issue),
												meme.get_remote_last_modified(),
												meme.get_local_last_modified()))
				if newest == "local":
					Log.debug("GTG->Gitlab updating td_task (%s, %s)" % (action, task.get_title()))
					self._populate_gl_issue(task, gl_issue)
					meme.set_remote_last_modified(self._get_gl_issue_modified(gl_issue))
					meme.set_local_last_modified(task_modified_alt)
				else:
					# we skip saving the state
					return

			elif action == SyncEngine.REMOVE:
				#Log.debug("GTG<-TD removing GTG task %s %s" %(action, task.get_title()))
				Log.debug("Skipping removal for now %s url: %s" %(action, url))

				#try:
					#self.sync_engine.break_relationship(local_id=tid)
					#self.datastore.request_task_deletion(tid)
				#except KeyError:
					#pass

			elif action == SyncEngine.LOST_SYNCABILITY:
				#Log.debug("Action: %s url: %s" %(action, url))
				try:
					gl_issue_iid = task.get_attribute("gl_issue_iid")
				except KeyError:
                	# in this case, we don't have yet the task in our local cache
                	# of what's on the gitlab website 
					#pass

					Log.debug("Couldn't find gl_issue_iid for that task..Getting relative issue from web")
					gl_issues_new = self._project.issues.list() #Network Call
					for gl_issue_new in gl_issues_new:
						if str(gl_issue_new.web_url) == url:
							self._exec_lost_syncability(tid, gl_issue)	
				finally:
					gl_issue = self._project.issues.get(gl_issue_iid)
					if gl_issue:
						self._exec_lost_syncability(tid, gl_issue)

			self.save_state()


	def _populate_gl_issue(self, task, gl_issue):
		# Title
		gl_issue.title= task.get_title()

		# Text
		text = task.get_excerpt(strip_tags=True, strip_subtasks=True)
		if str(gl_issue.description) != text:
			gl_issue.description = text

		# dates
		#gl_issue.updated_at = task.get_modified()
		dt = task.get_due_date()
		if dt:
			gl_issue.due_date = dt.strftime("%Y-%m-%d")
			
		# Status
		status = task.get_status()
		if status == Task.STA_ACTIVE:
			if str(gl_issue.state) == "closed":
				gl_issue.state_event = 'reopen'

		elif Task.STA_DONE:
			if str(gl_issue.state) != "closed":
				gl_issue.state_event = 'close'


		#Tags
		gtg_tags = set([self._tag_from_gtg(tag.get_name().lower()) for tag in task.get_tags()])
		gl_issue_labels = self._get_gl_issue_tags(gl_issue)
		gl_issue_tags = set(self._get_gl_issue_tags(gl_issue))
		# tags to add
		for tag in gtg_tags.difference(gl_issue_tags):
			gl_issue_labels.append(tag)
			gl_issue.labels = gl_issue_labels 
		# tags to remove
		for tag in gl_issue_tags.difference(gtg_tags):
			gl_issue_labels.remove(tag)
			gl_issue.labels = gl_issue_labels

		gl_issue.save()


		#Setting missing attributes when GTG creates a task first time
		if not task.get_attribute("gl_issue_iid"):
			task.set_attribute("web_url", str(gl_issue.web_url))
			task.set_attribute("gl_issue_id", gl_issue.id)
			task.set_attribute("gl_issue_iid", gl_issue.iid)
			remote_modified_dt = self._gl_date_to_datetime(str(gl_issue.updated_at))
			task.set_attribute("gl_issue_modified", remote_modified_dt.strftime("%Y-%m-%dT%H:%M:%S"))


###############################################################################
# Helper methods ##############################################################
###############################################################################
	def _exec_lost_syncability(self, tid, gl_issue):
		"""
		Executed when a relationship between tasks loses its syncability
		property. See SyncEngine for an explanation of that.

		@param tid: a GTG task tid
		@param note: a Todo
		"""
		self.cancellation_point()
		meme = self.sync_engine.get_meme_from_local_id(tid)
		# First of all, the relationship is lost
		self.sync_engine.break_relationship(local_id=tid)
		if meme.get_origin() == "GTG":
			self._project.issues.delete(gl_issue.iid)
		else:
			self.datastore.request_task_deletion(tid)


	def _get_task_modified_alt(self, tid):
		doc, xmlproj = cleanxml.openxmlfile(self.get_path(), "project")
		for node in xmlproj.childNodes:
			task_id = node.getAttribute("id")
			if task_id == tid:
				modified = taskxml.read_node(node, "modified")
				if modified != "":
					modified = datetime.datetime.strptime(modified, "%Y-%m-%dT%H:%M:%S")
					return Date(modified)
				
		return Date.no_date()

	def _gl_issue_is_syncable_per_attached_tags(self, gl_issue):
		"""
		Helper function which checks if the given task satisfies the filtering
		imposed by the tags attached to the backend.
		That means, if a user wants a backend to sync only tasks tagged @works,
		this function should be used to check if that is verified.

		@returns bool: True if the task should be synced
		"""
		attached_tags = self.get_attached_tags()
		attached_tags = [str(t) for t in attached_tags]
		if GenericBackend.ALLTASKS_TAG in attached_tags:
			return True

		tags = self._get_gl_issue_tags(gl_issue)
		for tag in [self._tag_to_gtg(tag) for tag in tags]:
			if tag in attached_tags:
				return True
		return False


	def _has_gl_issue(self, url):
		if url in [str(gl_issue.web_url) for gl_issue in self._gl_issues]:
			return True
		return False



	def _get_gl_issue_status (self, gl_issue):
		if str(gl_issue.state) == "closed":
			return Task.STA_DONE
		elif str(gl_issue.state) == "opened":
			return Task.STA_ACTIVE
		else:
			return Task.STA_ACTIVE	



	def _tag_to_gtg(self, tag):
		tag = tag.replace(' ', '_')
		return "@"+ tag




	def _tag_from_gtg(self, tag):
		tag = tag.replace('@', '', 1)
		return tag



	def _get_gl_issue_tags(self, gl_issue):
		l = []
		if len(gl_issue.labels) > 0:
			for label in gl_issue.labels:
				l.append(str(label))
		
		return l




	def _create_gl_issue(self, task):
		issue = self._project.issues.create({"title": task.get_title()})
		return issue




	def _get_gl_issue_due_date(self, gl_issue):
		date_string = gl_issue.due_date
		if date_string:
			dt = self._gl_date_to_datetime(str(date_string))

			# Gtg/Gitlab due date only has date portion of datetime
			# So convert datetime.datetime to datetime.date
			dt.replace(tzinfo=None)
			dt = dt.date()

			return self._datetime_to_gtg(dt)
		else:
			return Date.no_date()




	def _get_gl_issue_modified(self, gl_issue):
		date_string = str(gl_issue.updated_at)
		if date_string:
			dt = self._gl_date_to_datetime(date_string)
			return self._datetime_to_gtg(dt)
		else:
			return Date.no_date()




	def _tz_utc_to_local(self, dt):
		dt = dt.replace(tzinfo=tzutc())
		dt = dt.astimezone(tzlocal())
		return dt.replace(tzinfo=None)




	def _tz_local_to_utc(self, dt):
		dt = dt.replace(tzinfo=tzlocal())
		dt = dt.astimezone(tzutc())
		return dt.replace(tzinfo=None)



	def _tz_aware(self, dt):
		return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None




	def _gl_date_to_datetime(self, gl_date):
		"""
		gl_date must be string here. ex: str(gl_issue.updated_at)

		"""
		dt = dateutil.parser.parse(gl_date)
		if self._tz_aware(dt):
			# We are getting utc datetime object
			# So we convert to local and return the datetime
			# _datetime_to_gtg then convert it to gtg date fotmat
			
			return self._tz_utc_to_local(dt)

		return dt


	def _datetime_to_gtg(self, dt):
		if dt is None:
			return Date.no_date()
		if isinstance(dt, datetime.datetime):
			#dt.replace(tzinfo=None)
			#dt = dt.date()
			pass
		elif isinstance(dt, datetime.date):
			pass
		else:
			raise AssertionError("wrong type for %r" % dt)
		return Date(dt)

	def _gtg_to_datetime(self, gtg_date):
		if gtg_date is None or gtg_date == Date.no_date():
		    return None
		elif isinstance(gtg_date, datetime.datetime):
		    return gtg_date
		elif isinstance(gtg_date, datetime.date):
		    return datetime.datetime.combine(gtg_date, datetime.time(0))
		elif isinstance(gtg_date, Date):
		    return datetime.datetime.combine(gtg_date.date(), datetime.time(0))
		else:
		    raise NotImplementedError("Unknown type for %r" % gtg_date)
		
