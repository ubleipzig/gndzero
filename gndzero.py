#!/usr/bin/env python
# coding: utf-8

from __future__ import print_function

from luigi.task import flatten
import datetime
import itertools
import luigi
import os
import random
import re
import slugify
import sqlite3
import string
import subprocess
import tempfile

tempfile.tempdir = '/media/mtc/Data/tmp'
HOME = '/media/mtc/Data/var/data'



def convert(name):
    """
    Convert CamelCase to underscore, http://stackoverflow.com/a/1176023/89391.
    """
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1-\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1-\2', s1).lower()


def which(program):
    """
    Return `None` if no executable can be found.
    """
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


def random_string(length=16):
    """
    Return a random string (upper and lowercase letters) of length `length`,
    defaults to 16.
    """
    return ''.join(random.choice(string.letters) for _ in range(length))


def random_tmp_path():
    """
    Return a random path, that is located under the system's tmp dir. This
    is just a path, nothing gets touched or created.
    """
    return os.path.join(tempfile.gettempdir(), 'tasktree-%s' % random_string())


class dbopen(object):
    """
    Simple context manager for sqlite3 databases. Commits everything at exit.

    Example:

    ::

        with dbopen('/tmp/test.db') as cursor:
            query = cursor.execute('SELECT * FROM items')
            result = query.fetchall()
            ...

    """
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.cursor = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.path)
        self.conn.text_factory = str
        self.cursor = self.conn.cursor()
        return self.cursor

    def __exit__(self, exc_class, exc, traceback):
        self.conn.commit()
        self.conn.close()


class DefaultTask(luigi.Task):
    """
    A default class for finc. Expects a TAG (SOURCE_ID) on the class, 
    that gets turned into a instance attribute by the 
    `luigi.Task` __metaclass__.
    """
    TAG = NotImplemented

    def _parameters(self):
        """
        Return the parameters names as set.
        """
        params = set()
        for k, v in self.__class__.__dict__.iteritems():
            if isinstance(v, luigi.Parameter):
                params.add(k)
        return params

    def fingerprint(self, default='artefact'):
        """
        The fingerprint of a task is a string consisting of the names
        and values of the parametes.
        """
        parts = ['%s-%s' % (p, slugify.slugify(unicode(getattr(self, p)))) 
                 for p in self._parameters()]
        fingerprint = '-'.join(parts)
        if len(fingerprint) == 0:
            fingerprint = default
        return fingerprint


    def path(self, filename=None, ext='tsv'):
        """ 
        Autogenerate a path based on some category (those are only
        conventions), the tag (source id) and the name of the class and a given
        extension.
        """
        if self.TAG == NotImplemented:
            raise ValueError('(Base)class must set TAG (source id).')

        klassname = convert(self.__class__.__name__)

        if filename is None:
            filename = '%s.%s' % (self.fingerprint(), ext)
        return os.path.join(HOME, str(self.TAG), klassname, filename)


class GNDTask(DefaultTask):
    TAG = 'gndzero'


class Executable(luigi.Task):
    """ Checks, whether an external executable is available.
    This task returns `None` as output, so if this task is
    used make sure you check your input."""
    name = luigi.Parameter()

    def run(self):
        """ Just complain explicitly about missing program."""
        if not which(self.name):
            raise Exception('external program %s required' % self.name)

    def complete(self):
        return which(self.name) is not None

    def output(self):
        return None


class GNDDump(GNDTask):
    """Download GND task."""

    date = luigi.DateParameter(default=datetime.date.today())

    def requires(self):
        return Executable(name='wget')

    def run(self):
        url = "http://datendienst.dnb.de/cgi-bin/mabit.pl?cmd=fetch&userID=opendata&pass=opendata&mabheft=GND.rdf.gz"
        stopover = random_tmp_path()
        command = """ wget "%s" -O %s """ % (url, stopover)
        print(command)
        code = subprocess.call([command], shell=True)
        if not code == 0:
            raise RuntimeError("Could not download GND dump.")
        luigi.File(stopover).move(self.output().fn)

    def output(self):
        return luigi.LocalTarget(path=self.path(ext='rdf.gz'))


class GNDExtract(GNDTask):
    """Extract the archive."""
    date = luigi.DateParameter(default=datetime.date.today())

    def requires(self):
        return GNDDump(date=self.date)

    def run(self):
        stopover = random_tmp_path()
        command = """ gunzip -c %s > %s """ % (self.input().fn, stopover)
        print(command)
        code = subprocess.call([command], shell=True)
        if not code == 0:
            raise RuntimeError("Could not download GND dump.")
        luigi.File(stopover).move(self.output().fn)

    def output(self):
        return luigi.LocalTarget(path=self.path(ext='rdf'))


class SqliteDB(GNDTask):
    """Turn the dump into a (id, content) sqlite3 db."""

    date = luigi.DateParameter(default=datetime.date.today())

    def requires(self):
        return GNDExtract(date=self.date)

    def run(self):
        stopover = random_tmp_path()
        with dbopen(stopover) as cursor:
            cursor.execute("""CREATE TABLE gnd 
                              (id text, content blob)""")
            cursor.execute("""CREATE INDEX IF NOT EXISTS idx_gnd_id ON gnd (id)""")

            with self.input().open() as handle:
                groups = itertools.groupby(handle, key=str.isspace)
                for i, (k, lines) in enumerate(groups):
                    if i % 10000 == 0:
                        print('Inserted %s rows.' % i)
                    if k:
                        continue
                    lines = map(string.strip, list(lines))
                    match = re.search("""rdf:about="http://d-nb.info/gnd/([0-9X-]+)">""", lines[0])
                    if match:
                        row = (match.group(1), '\n'.join(lines))
                        cursor.execute("INSERT INTO gnd VALUES (?, ?)", row)

        luigi.File(path=stopover).move(self.output().fn)


    def output(self):
        return luigi.LocalTarget(path=self.path(ext='db'))


if __name__ == '__main__':
    luigi.run()
