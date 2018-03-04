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

import io
import os
import logging
import binascii
import traceback
from logging.handlers import SMTPHandler
from logging.handlers import RotatingFileHandler
from logging import Formatter
from ConfigParser import RawConfigParser
from functools import wraps

from flask import Flask, request, session, abort, flash, redirect, url_for, g, jsonify
import jinja2


class Bargate(Flask):
	error = False

	class CsrfpException(Exception):
		pass

	def __init__(self, init_object_name):
		super(Bargate, self).__init__(init_object_name)

		self._init_config()
		self._init_logging()
		self._init_templates()
		self._init_debug()
		self._init_csrfp()
		self._init_htmlmin()

		# Get the sections of the shares config file
		self.sharesConfig = RawConfigParser()
		if os.path.exists(self.config['SHARES_CONFIG']):
			with io.open(self.config['SHARES_CONFIG'], mode='r', encoding='utf-8') as f:
				self.sharesConfig.readfp(f)
			self.sharesList = self.sharesConfig.sections()
		else:
			self.sharesList = []
			self.logger.warn("The shares config file '" + self.config['SHARES_CONFIG'] + "' does not exist, ignoring")

		# Check the config options
		self._init_check_config()

		with self.app_context():
			# Load the SMB library
			try:
				if self.config['SMB_LIBRARY'] == "pysmbc":
					from bargate.smb.pysmbc import BargateSMBLibrary
				elif self.config['SMB_LIBRARY'] == "pysmb":
					from bargate.smb.pysmb import BargateSMBLibrary
				else:
					raise Exception("SMB_LIBRARY is set to an unknown library")

				self.set_smb_library(BargateSMBLibrary())
			except Exception as ex:
				self.logger.error("Could not load the SMB library: " + str(type(ex)) + " " + str(ex))
				self.logger.error(traceback.format_exc())
				self.error = "Could not load the SMB library: " + str(type(ex)) + " " + str(ex)

			# process per-request decorators
			from bargate import request # noqa

			# process view functions decorators
			from bargate.views import main, userdata, errors, smb # noqa

			# optionally load TOTP support
			if self.config['TOTP_ENABLED']:
				if self.config['REDIS_ENABLED']:
					from bargate.views import totp # noqa
				else:
					self.error = "Cannot enable TOTP 2-factor auth because REDIS is not enabled"

			if not self.error:
				# add url rules for the shares/endpoints
				for section in self.sharesList:
					if section == 'custom':
						self.logger.error("Could not create endpoint 'custom': name is reserved")
						continue

					if not self.sharesConfig.has_option(section, 'url'):
						url = '/' + section
					else:
						url = self.sharesConfig.get(section, 'url')

						if not url.startswith('/'):
							url = "/" + url

					if not self.sharesConfig.has_option(section, 'path'):
						self.logger.error("Could not create endpoint '" + section + "': parameter 'path' is not set")
						continue

					try:
						# If the user goes to /endpoint/browse/ or /endpoint/browse
						self.add_url_rule(url + '/browse/', endpoint=section, view_func=smb.endpoint_handler,
											defaults={'action': 'browse', 'path': ''})

						self.add_url_rule(url + '/browse', endpoint=section, view_func=smb.endpoint_handler,
											defaults={'action': 'browse', 'path': ''})

						# If the user goes to /endpoint or /endpoint/
						self.add_url_rule(url, endpoint=section, view_func=smb.endpoint_handler,
											defaults={'path': '', 'action': 'browse'})

						self.add_url_rule(url + '/', endpoint=section, view_func=smb.endpoint_handler,
											defaults={'path': '', 'action': 'browse'})

						# If the user goes to /endpoint/browse/path/
						self.add_url_rule(url + '/browse/<path:path>', endpoint=section,
											view_func=smb.endpoint_handler,
											defaults={'action': 'browse'})

						self.add_url_rule(url + '/browse/<path:path>/', endpoint=section,
											view_func=smb.endpoint_handler,
											defaults={'action': 'browse'})

						# If the user goes to /endpoint/<action>/path/
						self.add_url_rule(url + '/<string:action>/<path:path>', endpoint=section,
											view_func=smb.endpoint_handler)

						self.add_url_rule(url + '/<string:action>/<path:path>/', endpoint=section,
											view_func=smb.endpoint_handler)

						self.logger.debug("Created endpoint named '" + section + "' available at " + url)

					except Exception as ex:
						self.logger.error("Could not create file share '" + section + "': " + str(ex))

	def _init_config(self):
		# Load the default config
		self.config.from_object("bargate.defaultcfg")

		# try to load config from various paths
		if os.path.isfile('/etc/bargate/bargate.conf'):
			self.config.from_pyfile('/etc/bargate/bargate.conf')
		elif os.path.isfile('/etc/bargate.conf'):
			self.config.from_pyfile('/etc/bargate.conf')
		elif os.path.isfile('/opt/bargate/bargate.conf'):
			self.config.from_pyfile('/opt/bargate/bargate.conf')
		else:
			raise IOError("Could not find a configuration file for bargate")

	def _init_logging(self):

		# Set up logging to file
		if self.config['FILE_LOG']:
			self.file_handler = RotatingFileHandler(self.config['LOG_DIR'] + '/' + self.config['LOG_FILE'],
				'a', self.config['LOG_FILE_MAX_SIZE'],
				self.config['LOG_FILE_MAX_FILES'])
			self.file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
			self.logger.addHandler(self.file_handler)

		# Set the log level based on debug flag
		if self.debug:
			self.logger.setLevel(logging.DEBUG)
			self.file_handler.setLevel(logging.DEBUG)
		else:
			self.logger.setLevel(logging.INFO)
			self.file_handler.setLevel(logging.INFO)

		# Output some startup info
		self.logger.info('bargate version ' + self.config['VERSION'] + ' starting')
		if self.debug:
			self.logger.info('bargate is running in DEBUG mode')

		# set up e-mail alert logging
		if self.config['EMAIL_ALERTS']:
			# Log to file where e-mail alerts are going to
			self.logger.info('fatal errors will generate e-mail to: ' + str(self.config['ADMINS']))

			# Create the mail handler
			smtp_handler = SMTPHandler(self.config['SMTP_SERVER'],
				self.config['EMAIL_FROM'],
				self.config['ADMINS'],
				self.config['EMAIL_SUBJECT'])

			# Set the minimum log level (errors) and set a formatter
			smtp_handler.setLevel(logging.ERROR)
			smtp_handler.setFormatter(Formatter("""A fatal error occured in Bargate.

Message type:       %(levelname)s
Location:           %(pathname)s:%(lineno)d
Module:             %(module)s
Function:           %(funcName)s
Time:               %(asctime)s
Logger Name:        %(name)s
Process ID:         %(process)d

Further Details:

%(message)s

This message was generated by a call to app.logger.error()

"""))

			self.logger.addHandler(smtp_handler)

	def _init_templates(self):
		# load user defined templates
		if self.config['LOCAL_TEMPLATE_DIR']:

			if os.path.isdir(self.config['LOCAL_TEMPLATE_DIR']):

				self.jinja_loader = jinja2.ChoiceLoader(
					[
						jinja2.FileSystemLoader(self.config['LOCAL_TEMPLATE_DIR']),
						self.jinja_loader,
					])

				self.logger.info('site-specific templates will be loaded from: ' + str(self.config['LOCAL_TEMPLATE_DIR']))

			else:
				self.logger.error('site-specific templates cannot be loaded because LOCAL_TEMPLATE_DIR is not a directory')

		# check for a local static dir, and set up favicon
		if self.config['LOCAL_STATIC_DIR']:
			if os.path.isdir(self.config['LOCAL_STATIC_DIR']):
				self.logger.info('site-specific static files will be served from: ' + str(self.config['LOCAL_STATIC_DIR']))

				# override the built in / default favicon if one exists
				if os.path.isfile(os.path.join(self.config['LOCAL_STATIC_DIR'], 'favicon.ico')):
					self.logger.info('site-specific favicon found')
					self.config['LOCAL_FAVICON'] = True
			else:
				self.config['LOCAL_STATIC_DIR'] = False
				self.logger.error('site-specific static files cannot be served because LOCAL_STATIC_DIR is not a directory')

	def _init_debug(self):
		if self.config['DEBUG_TOOLBAR']:
			self.debug = True
			from flask_debugtoolbar import DebugToolbarExtension
			DebugToolbarExtension(self)
			self.config['DEBUG_TB_INTERCEPT_REDIRECTS'] = False
			self.logger.info('debug toolbar enabled - DO NOT USE THIS ON PRODUCTION SYSTEMS!')

	def _init_csrfp(self):
		self._exempt_views = set()
		self.add_template_global(self.csrfp_token)
		self.before_request(self.csrfp_before_request)

	def _init_check_config(self):
		if len(self.config['ENCRYPT_KEY']) == 0:
			self.logger.error("ENCRYPT_KEY is not set, disabling bargate")
			self.error = "The ENCRYPT_KEY configuration option is not set"

		elif len(self.config['ENCRYPT_KEY']) != 32:
			self.logger.error("ENCRYPT_KEY must be exactly 32-characters long, disabling bargate")
			self.error = "The ENCRYPT_KEY configuration option must be exactly 32-characters long"

		elif len(self.config['SECRET_KEY']) == 0:
			self.logger.error("SECRET_KEY is not set, disabling bargate")
			self.error = "The SECRET_KEY configuration option is not set"

		elif self.config['AUTH_TYPE'] not in ['kerberos', 'krb5', 'smb', 'ldap']:
			self.logger.error("AUTH_TYPE is set to an unknown authentication type, disabling bargate")
			self.error = "The value of the AUTH_TYPE configuration option is invalid/incorrect"

		elif self.config['SMB_LIBRARY'] not in ['pysmbc', 'pysmb']:
			self.logger.error("SMB_LIBRARY is set to an unknown library, disabling bargate")
			self.error = "The value of the SMB_LIBRARY configuration option is invalid/incorrect"

	def _init_htmlmin(self):
		self.minify = None
		if self.config['MINIFY_HTML']:
			try:
				from htmlmin.main import minify
				self.minify = minify
				self.after_request(self.minify_response)
			except Exception:
				self.logger.info("Could not load htmlmin library; html minifier not enabled")

	def set_smb_library(self, lib):
		self.smblib = lib

	def set_response_type(self, response_type):
		"""This is a decorator function that sets the response type on a view, such that errors generated respond
		with the right type. If you set 'html' (the default) then a text/html response will be returned. If you set
		'json' then an application/json response will be returned.
		"""

		def decorator(f):
			@wraps(f)
			def decorated_function(*args, **kwargs):
				g.response_type = response_type
				return f(*args, **kwargs)
			return decorated_function
		return decorator

	def login_required(self, f):
		"""This is a decorator function that when called ensures the user has logged in.
		Usage is as such: @app.login_required
		"""
		@wraps(f)
		def decorated_function(*args, **kwargs):

			if not self.is_user_logged_in():
				if g.get('response_type', 'html') == 'json':
					return jsonify({'code': 401, 'msg': 'You must be logged in to do that'})
				else:
					flash('You must be logged in to do that', 'alert-danger')
					session['next'] = request.url  # store the current url we're on
					return redirect(url_for('login'))

			return f(*args, **kwargs)
		return decorated_function

	def is_user_logged_in(self):
		return session.get('logged_in', False)

	# CSRF Protection (csrfp) functionality

	def csrf_token(self):
		return self.csrfp_token()

	def csrfp_token(self):
		if '_csrfp_token' not in session:
			session['_csrfp_token'] = self.token()
		return session['_csrfp_token']

	def csrfp_before_request(self):
		"""Performs the checking of CSRF tokens. This check is skipped for the
		GET, HEAD, OPTIONS and TRACE methods within HTTP, and is also skipped
		for any function that has been added to _exempt_views by use of the
		csrfp_exempt decorator."""

		# Throw away requests with methods we don't support
		if request.method not in ('GET', 'HEAD', 'POST'):
			abort(405)

		# For methods that require CSRF checking
		if request.method == 'POST':
			# Get the function that is rendering the current view
			view = self.view_functions.get(request.endpoint)

			# Make sure we actually found a view function
			if view is not None:
				view_location = view.__module__ + '.' + view.__name__

				# If the view is not exempt
				if view_location not in self._exempt_views:
					token = session.get('_csrfp_token')

					if not token or token != request.form.get('_csrfp_token'):
						if self.is_user_logged_in():
							self.logger.warning('CSRF Protection alert: %s failed to present a valid POST token', session['username'])
						else:
							self.logger.warning('CSRF Protection alert: a non-logged in user failed to present a valid POST token')

						raise self.CsrfpException()

				else:
					self.logger.debug('View ' + view_location + ' is exempt from CSRF Protection')

	def csrfp_exempt(self, view):
		"""A decorator that can be used to exclude a view from CSRF validation.
		:param view: The view to be wrapped by the decorator.
		"""

		view_location = view.__module__ + '.' + view.__name__
		self._exempt_views.add(view_location)
		self.logger.debug('Added CSRF Protection exemption for ' + view_location)
		return view

	def token(self, bytes=32):
		"""Generates a random token. This code was derived from the
			proposed new 'token' functions in Python 3.6, see:
			https://bitbucket.org/sdaprano/secrets/"""

		return binascii.hexlify(os.urandom(bytes))

	def minify_response(self, response):
		if self.minify is not None:
			if response.content_type == u'text/html; charset=utf-8':
				response.direct_passthrough = False
				response.set_data(self.minify(response.get_data(as_text=True),
					remove_comments=True,
					remove_optional_attribute_quotes=False))
				return response
			return response
