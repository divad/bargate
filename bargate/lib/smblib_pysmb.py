#!/usr/bin/python
#
# This file is part of Bargate.
#
# Bargate is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Bargate is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Bargate.  If not, see <http://www.gnu.org/licenses/>.

from bargate import app
import string, os, io, sys, stat, pprint, urllib, re
from flask import Flask, send_file, request, session, g, redirect, url_for, abort, flash, make_response, jsonify, render_template
import bargate.lib.core
import bargate.lib.errors
import bargate.lib.userdata
import bargate.lib.mime

from smb.SMBConnection import SMBConnection
import socket
import tempfile

### Python imaging stuff
from PIL import Image
import glob
import StringIO
import traceback

class backend_pysmb:
	def _sfile_load(self,sfile,srv_path, path, func_name):
		entry = {'skip': False, 'name': sfile.filename, 'size': sfile.file_size }

		## Skip entries for 'this dir' and 'parent dir'
		if entry['name'] == '.':
			entry['skip'] = True
		if entry['name'] == '..':
			entry['skip'] = True

		## Build the path
		if len(path) == 0:
			entry['path'] = entry['name']
		else:
			entry['path'] = path + '/' + entry['name']

		## Hide hidden files if the user has selected to do so (the default)
		if not bargate.lib.userdata.get_show_hidden_files():
			## UNIX hidden files
			if entry['name'].startswith('.'):
				entry['skip'] = True

			## Office temporary files
			if entry['name'].startswith('~$'):
				entry['skip'] = True

			## Other horrible Windows files
			hidden_entries = ['desktop.ini', '$RECYCLE.BIN', 'RECYCLER', 'Thumbs.db']

			if entry['name'] in hidden_entries:
				entry['skip'] = True

		if entry['skip']:
			return entry

		# Directories
		if sfile.isDirectory:
			entry['type'] = 'dir'
			entry['icon'] = 'fa fa-fw fa-folder'
			entry['stat'] = url_for(func_name,path=entry['path'],action='stat')
			entry['open'] = url_for(func_name,path=entry['path'])

		# Files
		else:
			entry['type'] = 'file'

			## Generate 'mtype', 'mtype_raw' and 'icon'
			entry['icon'] = 'fa fa-fw fa-file-text-o'
			(entry['mtype'],entry['mtype_raw']) = bargate.lib.mime.filename_to_mimetype(entry['name'])
			entry['icon'] = bargate.lib.mime.mimetype_to_icon(entry['mtype_raw'])

			## Generate URLs to this file
			entry['stat']         = url_for(func_name,path=entry['path'],action='stat')
			entry['download']     = url_for(func_name,path=entry['path'],action='download')
			entry['open']         = entry['download']

			# modification time (last write)
			entry['mtime_raw'] = sfile.last_write_time
			entry['mtime']     = bargate.lib.core.ut_to_string(sfile.last_write_time)

			## Image previews
			if app.config['IMAGE_PREVIEW'] and entry['mtype_raw'] in bargate.lib.mime.pillow_supported:
				if int(entry['size']) <= app.config['IMAGE_PREVIEW_MAX_SIZE']:
					entry['img_preview'] = url_for(func_name,path=entry['path'],action='preview')

			## View-in-browser download type
			if bargate.lib.mime.view_in_browser(entry['mtype_raw']):
				entry['view'] = url_for(func_name,path=entry['path'],action='view')
				entry['open'] = entry['view']
	
		return entry

################################################################################

	def smb_action(self,srv_path,func_name,active=None,display_name="Home",action='browse',path=''):

		## ensure srv_path (the server URI and share) does not end with a trailing slash
		if srv_path.endswith('/'):
			srv_path = srv_path[:-1]

		## srv_path should always start with smb://, we don't support anything else.
		if not srv_path.startswith("smb://"):
			return bargate.lib.errors.stderr("Invalid server path","The server URL must start with smb://")

		## work out just the server name part of the URL
		url_parts   = srv_path.replace("smb://","").split('/')
		server_name = url_parts[0]

		#flash("URL_PARTS: " + str(url_parts),"alert-info")

		if len(url_parts) == 1:
			return bargate.lib.errors.stderr("Invalid server path","The server URL must include at least share name")
		else:
			share_name = url_parts[1]

			if len(url_parts) == 2:
				full_path = "/" + path
			else:
				share_name = url_parts[1]
				full_path = "/" + "/".join(url_parts[2:]) + "/" + path

		#flash("SERVER_NAME: " + str(server_name),"alert-info")
		#flash("SHARE NAME: " + str(share_name),"alert-info")
		#flash("PATH: " + str(path),"alert-info")

		## default the 'active' variable to the function name
		if active == None:
			active = func_name

		## The place to redirect to (the url) if an error occurs
		## This defaults to None (aka don't redirect, and just show an error)
		## because to do otherwise will lead to a redirect loop. (Fix #93 v1.4.1)
		error_redirect = None

		## The parent directory to redirect to - defaults to just the current function
		## name (the handler for this 'share' at the top level)
		parent_redirect = redirect(url_for(func_name))

		## Connect to the SMB server
		conn = SMBConnection(session['username'], bargate.lib.user.get_password(), socket.gethostname(), server_name, domain=app.config['SMB_WORKGROUP'], use_ntlm_v2 = True, is_direct_tcp=True)

		if not conn.connect(server_name,port=445,timeout=5):
			return bargate.lib.errors.stderr("Could not connect","Could not connect to the SMB server, authentication was unsuccessful")

		############################################################################
		## HTTP GET ACTIONS ########################################################
		# actions: download/view, browse, stat
		############################################################################

		if request.method == 'GET':
			## Check the path is valid
			try:
				bargate.lib.core.check_path(path)
			except ValueError as e:
				return bargate.lib.errors.invalid_path()

			## Log this activity
			app.logger.info('User "' + session['username'] + '" connected to "' + srv_path + '" using endpoint "' + func_name + '" and action "' + action + '" using GET and path "' + path + '" from "' + request.remote_addr + '" using ' + request.user_agent.string)

			## Work out if there is a parent directory
			## and work out the entry name (filename or directory name being browsed)
			if len(path) > 0:
				(parent_directory_path,seperator,entryname) = path.rpartition('/')
				## if seperator was not found then the first two strings returned will be empty strings
				if len(parent_directory_path) > 0:
					parent_directory = True
					## update the parent redirect with the correct path
					parent_redirect = redirect(url_for(func_name,path=parent_directory_path))
					error_redirect  = parent_redirect

				else:
					parent_directory = False

			else:
				parent_directory = False
				parent_directory_path = ""
				entryname = ""

			uri = srv_path + "/" + path

			## parent_directory is either True/False if there is one
			## entryname will either be the part after the last / or the full path
			## parent_directory_path will be empty string or the parent directory path

	################################################################################
	# DOWNLOAD OR 'VIEW' FILE
	################################################################################

			if action == 'download' or action == 'view':

				try:
					## Default to sending files as an 'attachment' ("Content-Disposition: attachment")
					attach = True

					try:
						sfile = conn.getAttributes(share_name,full_path)
					except Exception as ex:
						abort(400)

					## ensure item is a file
					if sfile.isDirectory:
						abort(400)
				
					## guess a mimetype
					(ftype,mtype) = bargate.lib.mime.filename_to_mimetype(entryname)

					## If the user requested to 'view' (don't download as an attachment) make sure we allow it for that filetype
					if action == 'view':
						if bargate.lib.mime.view_in_browser(mtype):
							attach = False

					## pysmb wants to /write/ to a file, rather than provide a file-like object to read from. EUGH.
					## so we need to write to a temporary file that Flask's send_file can then read from.
					tfile = tempfile.SpooledTemporaryFile(max_size=1048576)

					## Read data into the tempfile from SMB
					app.logger.debug("A")
					conn.retrieveFile(share_name,full_path,tfile)
					tfile.seek(0)

					## Send the file to the user
					app.logger.debug("B")
					resp = make_response(send_file(tfile,add_etags=False,as_attachment=attach,attachment_filename=entryname,mimetype=mtype))
					app.logger.debug("C")
					resp.headers['Content-length'] = sfile.file_size
					app.logger.debug("D")
					return resp
	
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)

	################################################################################
	# IMAGE PREVIEW
	################################################################################
		
			elif action == 'preview':
				if not app.config['IMAGE_PREVIEW']:
					abort(400)

				try:
					sfile = conn.getAttributes(share_name,full_path)
				except Exception as ex:
					abort(400)

				## ensure item is a file
				if sfile.isDirectory:
					abort(400)
			
				## guess a mimetype
				(ftype,mtype) = bargate.lib.mime.filename_to_mimetype(entryname)
		
				## Check size is not too large for a preview
				if sfile.file_size > app.config['IMAGE_PREVIEW_MAX_SIZE']:
					abort(403)
			
				## Only preview files that Pillow supports
				if not mtype in bargate.lib.mime.pillow_supported:
					abort(400)

				## read the file
				tfile = tempfile.SpooledTemporaryFile(max_size=1048576)
				conn.retrieveFile(share_name,full_path,tfile)
				tfile.seek(0)

				## Read the file into memory first (hence a file size limit) because PIL/Pillow tries readline()
				## on pysmbc's File like objects which it doesn't support
				try:
					pil_img = Image.open(tfile).convert('RGB')
					size = 200, 200
					pil_img.thumbnail(size, Image.ANTIALIAS)

					ifile = StringIO.StringIO()
					pil_img.save(ifile, 'JPEG', quality=85)
					ifile.seek(0)
					return send_file(ifile, mimetype='image/jpeg',add_etags=False)
				except Exception as ex:
					abort(400)

	################################################################################
	# STAT FILE/DIR - json ajax request
	################################################################################
			
			elif action == 'stat': 

				try:
					sfile = conn.getAttributes(share_name,full_path)
				except Exception as ex:
					return jsonify({'error': 1, 'reason': 'An error occured: ' + str(type(ex)) + ": " + str(ex)})

				## ensure item is a file
				if sfile.isDirectory:
					return jsonify({'error': 1, 'reason': 'You cannot stat a directory!'})

				data = {}	
				data['filename']              = sfile.filename
				data['size']                  = sfile.file_size
				data['atime']                 = bargate.lib.core.ut_to_string(sfile.last_access_time)
				data['mtime']                 = bargate.lib.core.ut_to_string(sfile.last_write_time)
				(data['ftype'],data['mtype']) = bargate.lib.mime.filename_to_mimetype(data['filename'])
				data['owner'] = "Not yet implemented"
				data['group'] = "Not yet implemented"
				data['error'] = 0

				## Return JSON
				return jsonify(data)

	################################################################################
	# REALLY REALLY BASIC SEARCH...
	################################################################################
			
			elif action == 'search': #TODO
				if not app.config['SEARCH_ENABLED']:
					abort(404)

				if 'q' not in request.args:
					return redirect(url_for(func_name,path=path))

				## Build a breadcrumbs trail ##
				crumbs = []
				parts = path.split('/')
				b4 = ''

				## Build up a list of dicts, each dict representing a crumb
				for crumb in parts:
					if len(crumb) > 0:
						crumbs.append({'name': crumb, 'url': url_for(func_name,path=b4+crumb)})
						b4 = b4 + crumb + '/'

				query   = request.args.get('q')

				self._init_search(libsmbclient,func_name,path,path_as_str,srv_path_as_str,uri_as_str,query)
				results, timeout_reached = self._search()

				if timeout_reached:
					flash("Some search results have been omitted because the search took too long to perform.","alert-warning")

				return render_template('search.html',
					results=results,
					query=query,
					path=path,
					root_display_name = display_name,
					search_mode=True,
					url_home=url_for(func_name),
					crumbs=crumbs,
					on_file_click=bargate.lib.userdata.get_on_file_click())
			
	################################################################################
	# BROWSE / DIRECTORY / LIST FILES
	################################################################################
		
			elif action == 'browse':

				try:
					directory_entries = conn.listPath(share_name,full_path)
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)

				## Seperate out dirs and files into two lists
				dirs  = []
				files = []

				# sfile = shared file (smb.base.SharedFile)
				for sfile in directory_entries:
					entry = self._sfile_load(sfile, srv_path, path, func_name)

					# Don't add hidden files
					if not entry['skip']:
						if entry['type'] == 'file':
							files.append(entry)
						elif entry['type'] == 'dir':
							dirs.append(entry)

				## Build a breadcrumbs trail ##
				crumbs = []
				parts  = path.split('/')
				b4     = ''

				## Build up a list of dicts, each dict representing a crumb
				for crumb in parts:
					if len(crumb) > 0:
						crumbs.append({'name': crumb, 'url': url_for(func_name,path=b4+crumb)})
						b4 = b4 + crumb + '/'

				## Are we at the root?
				if len(path) == 0:
					atroot = True
				else:
					atroot = False
				
				## are there any items?
				no_items = False
				if len(files) == 0 and len(dirs) == 0:
					no_items = True

				## What layout does the user want?
				layout = bargate.lib.userdata.get_layout()

				## Render the template
				return render_template('directory-' + layout + '.html',
					active=active,
					dirs=dirs,
					files=files,
					crumbs=crumbs,
					path=path,
					cwd=entryname,
					url_home=url_for(func_name),
					url_parent_dir=url_for(func_name,path=parent_directory_path),
					url_bookmark=url_for('bookmarks'),
					url_search=url_for(func_name,path=path,action="search"),
					browse_mode=True,
					atroot = atroot,
					func_name = func_name,
					root_display_name = display_name,
					on_file_click=bargate.lib.userdata.get_on_file_click(),
					no_items = no_items,
				)

			else:
				abort(400)

		############################################################################
		## HTTP POST ACTIONS #######################################################
		# actions: unlink, mkdir, upload, rename
		############################################################################

		elif request.method == 'POST':

			## We ignore an action and/or path sent in the URL
			## this is because we send them both via form variables
			## we do this because we need, in javascript, to be able to change these
			## without having to regenerate the URL in the <form>
			## as such, the path and action are not sent via bargate POSTs anyway

			## Get the action and path
			action = request.form['action']
			path   = request.form['path']
		
			## Check the path is valid
			try:
				bargate.lib.core.check_path(path)
			except ValueError as e:
				return bargate.lib.errors.invalid_path()

			## pysmbc needs urllib quoted str objects, not unicode objects
			path_as_str = urllib.quote(path.encode('utf-8'))

			## Build the URI
			uri        = srv_path + path
			uri_as_str = srv_path_as_str + path_as_str

			## Log this activity
			app.logger.info('User "' + session['username'] + '" connected to "' + srv_path + '" using func name "' + func_name + '" and action "' + action + '" using POST and path "' + path + '" from "' + request.remote_addr + '" using ' + request.user_agent.string)


			## Work out if there is a parent directory
			## and work out the entry name (filename or directory name being browsed)
			if len(path) > 0:
				(parent_directory_path,seperator,entryname) = path.rpartition('/')
				## if seperator was not found then the first two strings returned will be empty strings
				if len(parent_directory_path) > 0:
					parent_directory = True
					parent_directory_path_as_str = urllib.quote(parent_directory_path.encode('utf-8'))
					parent_redirect = redirect(url_for(func_name,path=parent_directory_path))
					error_redirect = parent_redirect
				else:
					parent_directory = False

			else:
				parent_directory = False
				parent_directory_path = ""

			## parent_directory is either True/False if there is one
			## entryname will either be the part after the last / or the full path
			## parent_directory_path will be empty string or the parent directory path

	################################################################################
	# UPLOAD FILE
	################################################################################

			if action == 'jsonupload':
		
				ret = []
			
				uploaded_files = request.files.getlist("files[]")
			
				for ufile in uploaded_files:
			
					if bargate.lib.core.banned_file(ufile.filename):
						ret.append({'name' : ufile.filename, 'error': 'Filetype not allowed'})
						continue
					
					## Make the filename "secure" - see http://flask.pocoo.org/docs/patterns/fileuploads/#uploading-files
					filename = bargate.lib.core.secure_filename(ufile.filename)
					upload_uri_as_str = uri_as_str + '/' + urllib.quote(filename.encode('utf-8'))

					## Check the new file name is valid
					try:
						bargate.lib.core.check_name(filename)
					except ValueError as e:
						ret.append({'name' : ufile.filename, 'error': 'Filename not allowed'})
						continue
					
					## Check to see if the file exists
					fstat = None
					try:
						fstat = libsmbclient.stat(upload_uri_as_str)
					except smbc.NoEntryError:
						app.logger.debug("Upload filename of " + upload_uri_as_str + " does not exist, ignoring")
						## It doesn't exist so lets continue to upload
					except Exception as ex:
						#app.logger.error("Exception when uploading a file: " + str(type(ex)) + ": " + str(ex) + traceback.format_exc())
						ret.append({'name' : ufile.filename, 'error': 'Failed to stat existing file: ' + str(ex)})
						continue

					byterange_start = 0
					if 'Content-Range' in request.headers:
						byterange_start = int(request.headers['Content-Range'].split(' ')[1].split('-')[0])
						app.logger.debug("Chunked file upload request: Content-Range sent with byte range start of " + str(byterange_start) + " with filename " + filename)

					## Actual upload
					try:
						# Check if we're writing from the start of the file
						if byterange_start == 0:
							## We're truncating an existing file, or creating a new file
							## If the file already exists, check to see if we should overwrite
							if fstat is not None:
								if not bargate.lib.userdata.get_overwrite_on_upload():
									ret.append({'name' : ufile.filename, 'error': 'File already exists. You can enable overwriting files in Settings.'})
									continue

								## Now ensure we're not trying to upload a file on top of a directory (can't do that!)
								itemType = self.etype(libsmbclient,upload_uri_as_str)
								if itemType == SMB_DIR:
									ret.append({'name' : ufile.filename, 'error': "That name already exists and is a directory"})
									continue

							## Open the file for the first time, truncating or creating it if necessary
							app.logger.debug("Opening for writing with O_CREAT and TRUNC")
							wfile = libsmbclient.open(upload_uri_as_str,os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
						else:
							## Open the file and seek to where we are going to write the additional data
							app.logger.debug("Opening for writing with O_WRONLY")
							wfile = libsmbclient.open(upload_uri_as_str,os.O_WRONLY)
							wfile.seek(byterange_start)

						while True:
							buff = ufile.read(io.DEFAULT_BUFFER_SIZE)
							if not buff:
								break
							wfile.write(buff)

						wfile.close()
						ret.append({'name' : ufile.filename})

					except Exception as ex:
						#app.logger.error("Exception when uploading a file: " + str(type(ex)) + ": " + str(ex) + traceback.format_exc())
						ret.append({'name' : ufile.filename, 'error': 'Could not upload file: ' + str(ex)})
						continue
					
				return jsonify({'files': ret})

	################################################################################
	# RENAME FILE
	################################################################################

			elif action == 'rename':

				## Get the new requested file name
				new_filename = request.form['newfilename']

				## Check the new file name is valid
				try:
					bargate.lib.core.check_name(new_filename)
				except ValueError as e:
					return bargate.lib.errors.invalid_name()

				## build new URI
				new_filename_as_str = urllib.quote(new_filename.encode('utf-8'))
				if parent_directory:
					new_uri_as_str = srv_path_as_str + parent_directory_path_as_str + '/' + new_filename_as_str
				else:
					new_uri_as_str = srv_path_as_str + new_filename_as_str

				## get the item type of the existing 'filename'
				itemType = self._etype(libsmbclient,uri_as_str)

				if itemType == SMB_FILE:
					typemsg = "The file"
				elif itemType == SMB_DIR:
					typemsg = "The directory"
				else:
					return bargate.lib.errors.invalid_item_type(error_redirect)

				try:
					libsmbclient.rename(uri_as_str,new_uri_as_str)
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)
				else:
					flash(typemsg + " '" + entryname + "' was renamed to '" + request.form['newfilename'] + "' successfully.",'alert-success')
					return parent_redirect

	################################################################################
	# COPY FILE
	################################################################################

			elif action == 'copy':

				try:
					## stat the source file first
					source_stat = libsmbclient.stat(uri_as_str)

					## size of source
					source_size = source_stat[6]

					## determine item type
					itemType = self._stype(source_stat)

					## ensure item is a file
					if not itemType == SMB_FILE:
						return bargate.lib.errors.invalid_item_copy(error_redirect)

				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)

				## Get the new filename
				dest_filename = request.form['filename']
			
				## Check the new file name is valid
				try:
					bargate.lib.core.check_name(request.form['filename'])
				except ValueError as e:
					return bargate.lib.errors.invalid_name(error_redirect)
			
				## encode the new filename and quote the new filename
				if parent_directory:
					dest = srv_path_as_str + parent_directory_path_as_str + '/' + urllib.quote(dest_filename.encode('utf-8'))
				else:
					dest = srv_path_as_str + urllib.quote(dest_filename.encode('utf-8'))

				## Make sure the dest file doesn't exist
				try:
					libsmbclient.stat(dest)
				except smbc.NoEntryError as ex:
					## This is what we want - i.e. no file/entry
					pass
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)

				## Assuming we got here without an exception, open the source file
				try:		
					source_fh = libsmbclient.open(uri_as_str)
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)

				## Assuming we got here without an exception, open the dest file
				try:		
					dest_fh = libsmbclient.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC )

				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,srv_path + dest,error_redirect)

				## try reading then writing blocks of data, then redirect!
				try:
					location = 0
					while(location >= 0 and location < source_size):
						chunk = source_fh.read(1024)
						dest_fh.write(chunk)
						location = source_fh.seek(1024,location)

				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,srv_path + dest,error_redirect)

				flash('A copy of "' + entryname + '" was created as "' + dest_filename + '"','alert-success')
				return parent_redirect

	################################################################################
	# MAKE DIR
	################################################################################

			elif action == 'mkdir':
				## Check the path is valid
				try:
					bargate.lib.core.check_name(request.form['directory_name'])
				except ValueError as e:
					return bargate.lib.errors.invalid_name(error_redirect)

				mkdir_uri = uri_as_str + '/' + urllib.quote(request.form['directory_name'].encode('utf-8'))

				try:
					libsmbclient.mkdir(mkdir_uri,0755)
				except Exception as ex:
					return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)
				else:
					flash("The folder '" + request.form['directory_name'] + "' was created successfully.",'alert-success')
					return redirect(url_for(func_name,path=path))

	################################################################################
	# DELETE FILE
	################################################################################

			elif action == 'unlink':
				uri = uri.encode('utf-8')

				## get the item type of the entry we've been asked to delete
				itemType = self._etype(libsmbclient,uri_as_str)

				if itemType == SMB_FILE:
					try:
						libsmbclient.unlink(uri_as_str)
					except Exception as ex:
						return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)
					else:
						flash("The file '" + entryname + "' was deleted successfully.",'alert-success')
						return parent_redirect

				elif itemType == SMB_DIR:
					try:
						libsmbclient.rmdir(uri_as_str)
					except Exception as ex:
						return bargate.lib.errors.smbc_handler(ex,uri,error_redirect)
					else:
						flash("The directory '" + entryname + "' was deleted successfully.",'alert-success')
						return parent_redirect
				else:
					return bargate.lib.errors.invalid_item_type(error_redirect)

			else:
				abort(400)

	############################################################################

################################################################################

	def _init_search(self,libsmbclient,func_name,path,path_as_str,srv_path_as_str,uri_as_str,query):
		self.libsmbclient    = libsmbclient
		self.func_name       = func_name
		self.path            = path
		self.path_as_str     = path_as_str
		self.srv_path_as_str = srv_path_as_str
		self.uri_as_str      = uri_as_str
		self.query           = query

		self.timeout_at      = int(time.time()) + app.config['SEARCH_TIMEOUT']
		self.timeout_reached = False
		self.results         = []

	def _search(self):
		self._rsearch(self.path,self.path_as_str,self.uri_as_str)
		return self.results, self.timeout_reached

	def _rsearch(self,path, path_as_str, uri_as_str):
		## Try getting directory contents of where we are
		app.logger.debug("_rsearch called to search: " + uri_as_str)
		try:
			directory_entries = self.libsmbclient.opendir(uri_as_str).getdents()
		except smbc.NotDirectoryError as ex:
			return

		except Exception as ex:
			app.logger.info("Search encountered an exception " + str(ex) + " " + str(type(ex)))
			return

		## now loop over each entry
		for dentry in directory_entries:

			## don't keep searching if we reach the timeout
			if self.timeout_reached:
				break;
			elif int(time.time()) >= self.timeout_at:
				self.timeout_reached = True
				break

			entry = self._direntry_load(dentry, self.srv_path_as_str, path, path_as_str)

			## Skip hidden files
			if entry['skip']:
				continue

			## Check if the filename matched
			if self.query.lower() in entry['name'].lower():
				app.logger.debug("_rsearch: Matched: " + entry['name'])
				#entry = bargate.lib.smb.processDentry(entry, self.libsmbclient, self.func_name)
				entry['parent_path'] = path
				entry['parent_url']  = url_for(self.func_name,path=path)
				self.results.append(entry)

			## Search subdirectories if we found one
			if entry['type'] == 'dir':
				if len(path) > 0:
					new_path        = path + "/" + entry['name']
					new_path_as_str = path_as_str + "/" + entry['name_as_str']
				else:
					new_path        = entry['name']
					new_path_as_str = entry['name_as_str']					

				self._rsearch(new_path, new_path_as_str, entry['uri_as_str'])
