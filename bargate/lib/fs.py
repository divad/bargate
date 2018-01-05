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

import os
import re
from unicodedata import normalize

from bargate import app


class EntryType:
	# these values are taken from libsmbclient/samba
	other = 0
	share = 3
	dir   = 7
	file  = 8
	link  = 9


def banned_filename(filename):
	"""Takes a filename string and checks to see if has a banned
	file extension. Returns True or False.
	"""

	if '.' not in filename:
		return False

	elif filename.rsplit('.', 1)[1] in app.config['BANNED_EXTENSIONS']:
		return True

	else:
		return False


def secure_filename(filename):
	r"""Pass it a filename and it will return a secure version of it. This
	filename can then safely be stored on a regular file system and passed
	to :func:`os.path.join`.  The filename returned is an ASCII only string
	for maximum portability.

	On windows system the function also makes sure that the file is not
	named after one of the special device files.

	>>> secure_filename("My cool movie.mov")
	'My_cool_movie.mov'
	>>> secure_filename("../../../etc/passwd")
	'etc_passwd'
	>>> secure_filename(u'i contain cool \xfcml\xe4uts.txt')
	'i_contain_cool_umlauts.txt'

	The function might return an empty filename.  It's your responsibility
	to ensure that the filename is unique and that you generate random
	filename if the function returned an empty one.

	This is a modified version of the werkzeug secure filename modified
	for bargate to allow spaces in filenames.

	.. versionadded:: 0.5

	:param filename: the filename to secure
	"""

	if isinstance(filename, unicode):
		filename = normalize('NFKD', filename).encode('ascii', 'ignore')

	for sep in os.path.sep, os.path.altsep:
		if sep:
			filename = filename.replace(sep, ' ')

	regex = re.compile(r'[^ A-Za-z0-9_.-]')
	filename = str(regex.sub('_', filename))

	# on windows a couple of special files are present in each folder.  We
	# have to ensure that the target file is not such a filename.  In
	# this case we prepend an underline
	windows_device_files = ('CON', 'AUX', 'COM1', 'COM2', 'COM3', 'COM4', 'LPT1', 'LPT2', 'LPT3', 'PRN', 'NUL')

	if os.name == 'nt' and filename and filename.split('.')[0].upper() in windows_device_files:
		filename = '_' + filename

	return filename


def check_name(name):
	"""This function checks for invalid characters in a folder or file name or similar
	strings. It checks for a range of characters and invalid conditions as defined
	by Microsoft here: http://msdn.microsoft.com/en-us/library/windows/desktop/aa365247(v=vs.85).aspx
	Raises an exception of ValueError if any failure condition is met by the string.
	"""

	# File names MUST NOT end in a space or a period (full stop)
	if name.endswith(' ') or name.endswith('.'):
		raise ValueError('File and folder names must not end in a space or period (full stop) character')

	# Run the file/folder name check through the generic path checker
	check_path(name)

	# banned characters which CIFS servers reject
	invalidchars = re.compile(r'[<>/\\\":\|\?\*\x00]')

	# Check for the chars
	if invalidchars.search(name):
		raise ValueError('Invalid characters found. You cannot use the following: < > \ / : " ? *')

	return name


def check_path(path):
	"""This function checks for invalid characters in an entire path. It checks to ensure
	that paths don't contain strings which manipulate the path e.g up path or similar.
	Raises an exception of ValueError if any failure condition is met by the string.
	"""

	if path.startswith(".."):
		raise ValueError('Invalid path. Paths cannot start with ".."')

	if path.startswith("./"):
		raise ValueError('Invalid path. Paths cannot start with "./"')

	if path.startswith(".\\"):
		raise ValueError('Invalid path. Paths cannot start with ".\"')

	if '/../' in path:
		raise ValueError('Invalid path. Paths cannot contain "/../"')

	if '\\..\\' in path:
		raise ValueError('Invalid path. Paths cannot contain "\..\"')

	if '\\.\\' in path:
		raise ValueError('Invalid path. Paths cannot contain "\.\"')

	if '/./' in path:
		raise ValueError('Invalid path. Paths cannot contain "/./"')

	return path