# vim:fileencoding=utf-8:noet
from __future__ import (unicode_literals, division, absolute_import, print_function)

import os
import sys
import re
import operator
import subprocess

from powerline.lib.vcs import get_branch_name, get_file_status
from powerline.lib.shell import readlines


_ref_pat = re.compile(br'ref:\s*refs/heads/(.+)')


def branch_name_from_config_file(directory, config_file):
	try:
		with open(config_file, 'rb') as f:
			raw = f.read()
	except EnvironmentError:
		return os.path.basename(directory)
	m = _ref_pat.match(raw)
	if m is not None:
		return m.group(1).decode('utf-8', 'replace')
	return raw[:7]


def git_directory(directory):
	path = os.path.join(directory, '.git')
	if os.path.isfile(path):
		with open(path, 'rb') as f:
			raw = f.read()
			if not raw.startswith(b'gitdir: '):
				raise IOError('invalid gitfile format')
			raw = raw[8:].decode(sys.getfilesystemencoding() or 'utf-8')
			if raw[-1] == '\n':
				raw = raw[:-1]
			if not raw:
				raise IOError('no path in gitfile')
			return os.path.abspath(os.path.join(directory, raw))
	else:
		return path


class GitRepository(object):
	__slots__ = ('directory', 'create_watcher')

	def __init__(self, directory, create_watcher):
		self.directory = os.path.abspath(directory)
		self.create_watcher = create_watcher

	def status(self, path=None):
		'''Return status of repository or file.

		Without file argument: returns status of the repository:

		:First column: working directory status (D: dirty / space)
		:Second column: index status (I: index dirty / space)
		:Third column: presence of untracked files (U: untracked files / space)
		:None: repository clean

		With file argument: returns status of this file. Output is
		equivalent to the first two columns of "git status --porcelain"
		(except for merge statuses as they are not supported by libgit2).
		'''
		if path:
			gitd = git_directory(self.directory)
			# We need HEAD as without it using fugitive to commit causes the
			# current file's status (and only the current file) to not be updated
			# for some reason I cannot be bothered to figure out.
			return get_file_status(
				directory=self.directory,
				dirstate_file=os.path.join(gitd, 'index'),
				file_path=path,
				ignore_file_name='.gitignore',
				get_func=self.do_status,
				create_watcher=self.create_watcher,
				extra_ignore_files=tuple(os.path.join(gitd, x) for x in ('logs/HEAD', 'info/exclude')),
			)
		return self.do_status(self.directory, path)

	def branch(self):
		directory = git_directory(self.directory)
		head = os.path.join(directory, 'HEAD')
		return get_branch_name(
			directory=directory,
			config_file=head,
			get_func=branch_name_from_config_file,
			create_watcher=self.create_watcher,
		)


try:
	import pygit2 as git

	class Repository(GitRepository):
		@staticmethod
		def ignore_event(path, name):
			return False

		def do_status(self, directory, path):
			if path:
				try:
					status = git.Repository(directory).status_file(path)
				except (KeyError, ValueError):
					return None

				if status == git.GIT_STATUS_CURRENT:
					return None
				else:
					if status & git.GIT_STATUS_WT_NEW:
						return '??'
					if status & git.GIT_STATUS_IGNORED:
						return '!!'

					if status & git.GIT_STATUS_INDEX_NEW:
						index_status = 'A'
					elif status & git.GIT_STATUS_INDEX_DELETED:
						index_status = 'D'
					elif status & git.GIT_STATUS_INDEX_MODIFIED:
						index_status = 'M'
					else:
						index_status = ' '

					if status & git.GIT_STATUS_WT_DELETED:
						wt_status = 'D'
					elif status & git.GIT_STATUS_WT_MODIFIED:
						wt_status = 'M'
					else:
						wt_status = ' '

					return index_status + wt_status
			else:
				raw_status = git.Repository(directory).status().values()
				repo_status = reduce(operator.or_, raw_status, 0)

				def check(mask, value):
					return (value & mask == mask)

				def checkall(ref, tag, flags):
					flagtypes  = ['new', 'modified', 'deleted']
					ret = ['%s:%s' % (tag, ftype) for (ftype, f) in zip(flagtypes, flags) if check(f, ref)]
					if ret == []:
						return ['%s:%s' % (tag, 'clean')]

					return ret + ['%s:%s' % (tag, 'dirty')]

				wt_status  = checkall(repo_status, 'working_tree', [
					git.GIT_STATUS_WT_NEW,
					git.GIT_STATUS_WT_MODIFIED,
					git.GIT_STATUS_WT_DELETED])
				idx_status = checkall(repo_status, 'index', [
					git.GIT_STATUS_INDEX_NEW,
					git.GIT_STATUS_INDEX_MODIFIED,
					git.GIT_STATUS_INDEX_DELETED])
				return idx_status + wt_status

		def aheadby_external(self, us, them):
			cmd = "git log --oneline {1}..{0}".format(us, them)
			process = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
			output = process.stdout.read()
			lines = output.splitlines()
			return len(lines)

		def divergence(self):
			'''Return number of commits divergence (if any) from the upstream
			'''
			repo = git.Repository(self.directory)
			# TODO This might not be a branch...
			branch = repo.lookup_branch(repo.head.shorthand)
			if branch is not None:
				upstream = "{0}@{{u}}".format(branch.shorthand)
				return ( self.aheadby_external(branch.shorthand, upstream), self.aheadby_external(upstream, branch.shorthand) )

except ImportError:
	class Repository(GitRepository):
		@staticmethod
		def ignore_event(path, name):
			# Ignore changes to the index.lock file, since they happen 
			# frequently and dont indicate an actual change in the working tree 
			# status
			return path.endswith('.git') and name == 'index.lock'

		def _gitcmd(self, directory, *args):
			return readlines(('git',) + args, directory)

		def do_status(self, directory, path):
			if path:
				try:
					return next(self._gitcmd(directory, 'status', '--porcelain', '--ignored', '--', path))[:2]
				except StopIteration:
					return None
			else:
				wt_column = ' '
				index_column = ' '
				untracked_column = ' '
				for line in self._gitcmd(directory, 'status', '--porcelain'):
					if line[0] == '?':
						untracked_column = 'U'
						continue
					elif line[0] == '!':
						continue

					if line[0] != ' ':
						index_column = 'I'

					if line[1] != ' ':
						wt_column = 'D'

				r = wt_column + index_column + untracked_column
				return r if r != '   ' else None

		def divergence(self):
			# Not convinced we can tell what 'divergence' means in this case
			return (0, 0)
