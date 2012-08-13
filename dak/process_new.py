#!/usr/bin/env python
# vim:set et ts=4 sw=4:

""" Handles NEW and BYHAND packages

@contact: Debian FTP Master <ftpmaster@debian.org>
@copyright: 2001, 2002, 2003, 2004, 2005, 2006  James Troup <james@nocrew.org>
@copyright: 2009 Joerg Jaspert <joerg@debian.org>
@copyright: 2009 Frank Lichtenheld <djpig@debian.org>
@license: GNU General Public License version 2 or later
"""
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

################################################################################

# 23:12|<aj> I will not hush!
# 23:12|<elmo> :>
# 23:12|<aj> Where there is injustice in the world, I shall be there!
# 23:13|<aj> I shall not be silenced!
# 23:13|<aj> The world shall know!
# 23:13|<aj> The world *must* know!
# 23:13|<elmo> oh dear, he's gone back to powerpuff girls... ;-)
# 23:13|<aj> yay powerpuff girls!!
# 23:13|<aj> buttercup's my favourite, who's yours?
# 23:14|<aj> you're backing away from the keyboard right now aren't you?
# 23:14|<aj> *AREN'T YOU*?!
# 23:15|<aj> I will not be treated like this.
# 23:15|<aj> I shall have my revenge.
# 23:15|<aj> I SHALL!!!

################################################################################

import copy
import errno
import os
import readline
import stat
import sys
import time
import contextlib
import pwd
import apt_pkg, apt_inst
import examine_package
import subprocess

from daklib.dbconn import *
from daklib.queue import *
from daklib import daklog
from daklib import utils
from daklib.regexes import re_no_epoch, re_default_answer, re_isanum, re_package
from daklib.dak_exceptions import CantOpenError, AlreadyLockedError, CantGetLockError
from daklib.summarystats import SummaryStats
from daklib.config import Config
from daklib.policy import UploadCopy, PolicyQueueUploadHandler

# Globals
Options = None
Logger = None

Priorities = None
Sections = None

################################################################################
################################################################################
################################################################################

class Section_Completer:
    def __init__ (self, session):
        self.sections = []
        self.matches = []
        for s, in session.query(Section.section):
            self.sections.append(s)

    def complete(self, text, state):
        if state == 0:
            self.matches = []
            n = len(text)
            for word in self.sections:
                if word[:n] == text:
                    self.matches.append(word)
        try:
            return self.matches[state]
        except IndexError:
            return None

############################################################

class Priority_Completer:
    def __init__ (self, session):
        self.priorities = []
        self.matches = []
        for p, in session.query(Priority.priority):
            self.priorities.append(p)

    def complete(self, text, state):
        if state == 0:
            self.matches = []
            n = len(text)
            for word in self.priorities:
                if word[:n] == text:
                    self.matches.append(word)
        try:
            return self.matches[state]
        except IndexError:
            return None

################################################################################

def print_new (upload, missing, indexed, session, file=sys.stdout):
    check_valid(missing, session)
    index = 0
    for m in missing:
        index += 1
        if m['type'] != 'deb':
            package = '{0}:{1}'.format(m['type'], m['package'])
        else:
            package = m['package']
        section = m['section']
        priority = m['priority']
        if indexed:
            line = "(%s): %-20s %-20s %-20s" % (index, package, priority, section)
        else:
            line = "%-20s %-20s %-20s" % (package, priority, section)
        line = line.strip()
        if not m['valid']:
            line = line + ' [!]'
        print >>file, line
    notes = get_new_comments(upload.changes.source)
    for note in notes:
        print "\nAuthor: %s\nVersion: %s\nTimestamp: %s\n\n%s" \
              % (note.author, note.version, note.notedate, note.comment)
        print "-" * 72
    return len(notes) > 0

################################################################################

def index_range (index):
    if index == 1:
        return "1"
    else:
        return "1-%s" % (index)

################################################################################
################################################################################

def edit_new (overrides, upload, session):
    # Write the current data to a temporary file
    (fd, temp_filename) = utils.temp_filename()
    temp_file = os.fdopen(fd, 'w')
    print_new (upload, overrides, indexed=0, session=session, file=temp_file)
    temp_file.close()
    # Spawn an editor on that file
    editor = os.environ.get("EDITOR","vi")
    result = os.system("%s %s" % (editor, temp_filename))
    if result != 0:
        utils.fubar ("%s invocation failed for %s." % (editor, temp_filename), result)
    # Read the edited data back in
    temp_file = utils.open_file(temp_filename)
    lines = temp_file.readlines()
    temp_file.close()
    os.unlink(temp_filename)

    overrides_map = dict([ ((o['type'], o['package']), o) for o in overrides ])
    new_overrides = []
    # Parse the new data
    for line in lines:
        line = line.strip()
        if line == "" or line[0] == '#':
            continue
        s = line.split()
        # Pad the list if necessary
        s[len(s):3] = [None] * (3-len(s))
        (pkg, priority, section) = s[:3]
        if pkg.find(':') != -1:
            type, pkg = pkg.split(':', 1)
        else:
            type = 'deb'
        if (type, pkg) not in overrides_map:
            utils.warn("Ignoring unknown package '%s'" % (pkg))
        else:
            if section.find('/') != -1:
                component = section.split('/', 1)[0]
            else:
                component = 'main'
            new_overrides.append(dict(
                    package=pkg,
                    type=type,
                    section=section,
                    component=component,
                    priority=priority,
                    ))
    return new_overrides

################################################################################

def edit_index (new, upload, index):
    package = new[index]['package']
    priority = new[index]["priority"]
    section = new[index]["section"]
    ftype = new[index]["type"]
    done = 0
    while not done:
        print "\t".join([package, priority, section])

        answer = "XXX"
        if ftype != "dsc":
            prompt = "[B]oth, Priority, Section, Done ? "
        else:
            prompt = "[S]ection, Done ? "
        edit_priority = edit_section = 0

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = re_default_answer.match(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

        if answer == 'P':
            edit_priority = 1
        elif answer == 'S':
            edit_section = 1
        elif answer == 'B':
            edit_priority = edit_section = 1
        elif answer == 'D':
            done = 1

        # Edit the priority
        if edit_priority:
            readline.set_completer(Priorities.complete)
            got_priority = 0
            while not got_priority:
                new_priority = utils.our_raw_input("New priority: ").strip()
                if new_priority not in Priorities.priorities:
                    print "E: '%s' is not a valid priority, try again." % (new_priority)
                else:
                    got_priority = 1
                    priority = new_priority

        # Edit the section
        if edit_section:
            readline.set_completer(Sections.complete)
            got_section = 0
            while not got_section:
                new_section = utils.our_raw_input("New section: ").strip()
                if new_section not in Sections.sections:
                    print "E: '%s' is not a valid section, try again." % (new_section)
                else:
                    got_section = 1
                    section = new_section

        # Reset the readline completer
        readline.set_completer(None)

    new[index]["priority"] = priority
    new[index]["section"] = section
    if section.find('/') != -1:
        component = section.split('/', 1)[0]
    else:
        component = 'main'
    new[index]['component'] = component

    return new

################################################################################

def edit_overrides (new, upload, session):
    print
    done = 0
    while not done:
        print_new (upload, new, indexed=1, session=session)
        prompt = "edit override <n>, Editor, Done ? "

        got_answer = 0
        while not got_answer:
            answer = utils.our_raw_input(prompt)
            if not answer.isdigit():
                answer = answer[:1].upper()
            if answer == "E" or answer == "D":
                got_answer = 1
            elif re_isanum.match (answer):
                answer = int(answer)
                if answer < 1 or answer > len(new):
                    print "{0} is not a valid index.  Please retry.".format(answer)
                else:
                    got_answer = 1

        if answer == 'E':
            new = edit_new(new, upload, session)
        elif answer == 'D':
            done = 1
        else:
            edit_index (new, upload, answer - 1)

    return new


################################################################################

def check_pkg (upload, upload_copy):
    save_stdout = sys.stdout
    changes = os.path.join(upload_copy.directory, upload.changes.changesname)
    suite_name = upload.target_suite.suite_name
    try:
        sys.stdout = os.popen("less -R -", 'w', 0)
        print examine_package.display_changes(suite_name, changes)

        source = upload.source
        if source is not None:
            source_file = os.path.join(upload_copy.directory, os.path.basename(source.poolfile.filename))
            print examine_package.check_dsc(suite_name, source_file)

        for binary in upload.binaries:
            binary_file = os.path.join(upload_copy.directory, os.path.basename(binary.poolfile.filename))
            print examine_package.check_deb(suite_name, binary_file)

        print examine_package.output_package_relations()
    except IOError as e:
        if e.errno == errno.EPIPE:
            utils.warn("[examine_package] Caught EPIPE; skipping.")
        else:
            raise
    except KeyboardInterrupt:
        utils.warn("[examine_package] Caught C-c; skipping.")
    finally:
        sys.stdout = save_stdout

################################################################################

## FIXME: horribly Debian specific

def do_bxa_notification(new, upload, session):
    cnf = Config()

    new = set([ o['package'] for o in new if o['type'] == 'deb' ])
    if len(new) == 0:
        return

    key = session.query(MetadataKey).filter_by(key='Description').one()
    summary = ""
    for binary in upload.binaries:
        if binary.package not in new:
            continue
        description = session.query(BinaryMetadata).filter_by(binary=binary, key=key).one().value
        summary += "\n"
        summary += "Package: {0}\n".format(binary.package)
        summary += "Description: {0}\n".format(description)

    subst = {
        '__DISTRO__': cnf['Dinstall::MyDistribution'],
        '__BCC__': 'X-DAK: dak process-new',
        '__BINARY_DESCRIPTIONS__': summary,
        }

    bxa_mail = utils.TemplateSubst(subst,os.path.join(cnf["Dir::Templates"], "process-new.bxa_notification"))
    utils.send_mail(bxa_mail)

################################################################################

def add_overrides (new_overrides, suite, session):
    if suite.overridesuite is not None:
        suite = session.query(Suite).filter_by(suite_name=suite.overridesuite).one()

    for override in new_overrides:
        package = override['package']
        priority = session.query(Priority).filter_by(priority=override['priority']).first()
        section = session.query(Section).filter_by(section=override['section']).first()
        component = get_mapped_component(override['component'], session)
        overridetype = session.query(OverrideType).filter_by(overridetype=override['type']).one()

        if priority is None:
            raise Exception('Invalid priority {0} for package {1}'.format(priority, package))
        if section is None:
            raise Exception('Invalid section {0} for package {1}'.format(section, package))
        if component is None:
            raise Exception('Invalid component {0} for package {1}'.format(component, package))

        o = Override(package=package, suite=suite, component=component, priority=priority, section=section, overridetype=overridetype)
        session.add(o)

    session.commit()

################################################################################

def run_user_inspect_command(upload, upload_copy):
    command = os.environ.get('DAK_INSPECT_UPLOAD')
    if command is None:
        return

    directory = upload_copy.directory
    if upload.source:
        dsc = os.path.basename(upload.source.poolfile.filename)
    else:
        dsc = ''
    changes = upload.changes.changesname

    shell_command = command.format(
            directory=directory,
            dsc=dsc,
            changes=changes,
            )

    subprocess.check_call(shell_command, shell=True)

################################################################################

def get_reject_reason(reason=''):
    """get reason for rejection

    @rtype:  str
    @return: string giving the reason for the rejection or C{None} if the
             rejection should be cancelled
    """
    answer = 'E'
    if Options['Automatic']:
        answer = 'R'

    while answer == 'E':
        reason = utils.call_editor(reason)
        print "Reject message:"
        print utils.prefix_multi_line_string(reason, "  ", include_blank_lines=1)
        prompt = "[R]eject, Edit, Abandon, Quit ?"
        answer = "XXX"
        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

    if answer == 'Q':
        sys.exit(0)

    if answer == 'R':
        return reason
    return None

################################################################################

def do_new(upload, upload_copy, handler, session):
    print "NEW\n"
    cnf = Config()

    run_user_inspect_command(upload, upload_copy)

    # The main NEW processing loop
    done = False
    missing = []
    while not done:
        queuedir = upload.policy_queue.path
        byhand = upload.byhand

        missing = handler.missing_overrides(hints=missing)
        broken = not check_valid(missing, session)

        #if len(byhand) == 0 and len(missing) == 0:
        #    break

        answer = "XXX"
        if Options["No-Action"] or Options["Automatic"]:
            answer = 'S'

        note = print_new(upload, missing, indexed=0, session=session)
        prompt = ""

        has_unprocessed_byhand = False
        for f in byhand:
            path = os.path.join(queuedir, f.filename)
            if not f.processed and os.path.exists(path):
                print "W: {0} still present; please process byhand components and try again".format(f.filename)
                has_unprocessed_byhand = True

        if not has_unprocessed_byhand and not broken and not note:
            if len(missing) == 0:
                prompt = "Accept, "
                answer = 'A'
            else:
                prompt = "Add overrides, "
        if broken:
            print "W: [!] marked entries must be fixed before package can be processed."
        if note:
            print "W: note must be removed before package can be processed."
            prompt += "RemOve all notes, Remove note, "

        prompt += "Edit overrides, Check, Manual reject, Note edit, Prod, [S]kip, Quit ?"

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

        if answer in ( 'A', 'E', 'M', 'O', 'R' ) and Options["Trainee"]:
            utils.warn("Trainees can't do that")
            continue

        if answer == 'A' and not Options["Trainee"]:
            try:
                check_daily_lock()
                add_overrides(missing, upload.target_suite, session)
                if Config().find_b("Dinstall::BXANotify"):
                    do_bxa_notification(missing, upload, session)
                handler.accept()
                done = True
                Logger.log(["NEW ACCEPT", upload.changes.changesname])
            except CantGetLockError:
                print "Hello? Operator! Give me the number for 911!"
                print "Dinstall in the locked area, cant process packages, come back later"
        elif answer == 'C':
            check_pkg(upload, upload_copy)
        elif answer == 'E' and not Options["Trainee"]:
            missing = edit_overrides (missing, upload, session)
        elif answer == 'M' and not Options["Trainee"]:
            reason = Options.get('Manual-Reject', '') + "\n"
            reason = reason + "\n".join([n.comment for n in get_new_comments(upload.changes.source, session=session)])
            reason = get_reject_reason(reason)
            if reason is not None:
                Logger.log(["NEW REJECT", upload.changes.changesname])
                handler.reject(reason)
                done = True
        elif answer == 'N':
            if edit_note(get_new_comments(upload.changes.source, session=session),
                         upload, session, bool(Options["Trainee"])) == 0:
                end()
                sys.exit(0)
        elif answer == 'P' and not Options["Trainee"]:
            if prod_maintainer(get_new_comments(upload.changes.source, session=session),
                               upload) == 0:
                end()
                sys.exit(0)
            Logger.log(["NEW PROD", upload.changes.changesname])
        elif answer == 'R' and not Options["Trainee"]:
            confirm = utils.our_raw_input("Really clear note (y/N)? ").lower()
            if confirm == "y":
                for c in get_new_comments(upload.changes.source, upload.changes.version, session=session):
                    session.delete(c)
                session.commit()
        elif answer == 'O' and not Options["Trainee"]:
            confirm = utils.our_raw_input("Really clear all notes (y/N)? ").lower()
            if confirm == "y":
                for c in get_new_comments(upload.changes.source, session=session):
                    session.delete(c)
                session.commit()

        elif answer == 'S':
            done = True
        elif answer == 'Q':
            end()
            sys.exit(0)

################################################################################
################################################################################
################################################################################

def usage (exit_code=0):
    print """Usage: dak process-new [OPTION]... [CHANGES]...
  -a, --automatic           automatic run
  -b, --no-binaries         do not sort binary-NEW packages first
  -c, --comments            show NEW comments
  -h, --help                show this help and exit.
  -m, --manual-reject=MSG   manual reject with `msg'
  -n, --no-action           don't do anything
  -t, --trainee             FTP Trainee mode
  -V, --version             display the version number and exit

ENVIRONMENT VARIABLES

  DAK_INSPECT_UPLOAD: shell command to run to inspect a package
      The command is automatically run in a shell when an upload
      is checked.  The following substitutions are available:

        {directory}: directory the upload is contained in
        {dsc}:       name of the included dsc or the empty string
        {changes}:   name of the changes file

      Note that Python's 'format' method is used to format the command.

      Example: run mc in a tmux session to inspect the upload

      export DAK_INSPECT_UPLOAD='tmux new-session -d -s process-new 2>/dev/null; tmux new-window -n "{changes}" -t process-new:0 -k "cd {directory}; mc"'

      and run

      tmux attach -t process-new

      in a separate terminal session.
"""
    sys.exit(exit_code)

################################################################################

def check_daily_lock():
    """
    Raises CantGetLockError if the dinstall daily.lock exists.
    """

    cnf = Config()
    try:
        lockfile = cnf.get("Process-New::DinstallLockFile",
                           os.path.join(cnf['Dir::Lock'], 'processnew.lock'))

        os.open(lockfile,
                os.O_RDONLY | os.O_CREAT | os.O_EXCL)
    except OSError as e:
        if e.errno == errno.EEXIST or e.errno == errno.EACCES:
            raise CantGetLockError

    os.unlink(lockfile)

@contextlib.contextmanager
def lock_package(package):
    """
    Lock C{package} so that noone else jumps in processing it.

    @type package: string
    @param package: source package name to lock
    """

    cnf = Config()

    path = os.path.join(cnf.get("Process-New::LockDir", cnf['Dir::Lock']), package)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDONLY)
    except OSError as e:
        if e.errno == errno.EEXIST or e.errno == errno.EACCES:
            user = pwd.getpwuid(os.stat(path)[stat.ST_UID])[4].split(',')[0].replace('.', '')
            raise AlreadyLockedError(user)

    try:
        yield fd
    finally:
        os.unlink(path)

def do_pkg(upload, session):
    # Try to get an included dsc
    dsc = upload.source

    cnf = Config()
    #bcc = "X-DAK: dak process-new"
    #if cnf.has_key("Dinstall::Bcc"):
    #    u.Subst["__BCC__"] = bcc + "\nBcc: %s" % (cnf["Dinstall::Bcc"])
    #else:
    #    u.Subst["__BCC__"] = bcc

    try:
      with lock_package(upload.changes.source):
       with UploadCopy(upload) as upload_copy:
        handler = PolicyQueueUploadHandler(upload, session)
        if handler.get_action() is not None:
            return

        do_new(upload, upload_copy, handler, session)
    except AlreadyLockedError as e:
        print "Seems to be locked by %s already, skipping..." % (e)

def show_new_comments(uploads, session):
    sources = [ upload.changes.source for upload in uploads ]
    if len(sources) == 0:
        return

    query = """SELECT package, version, comment, author
               FROM new_comments
               WHERE package IN :sources
               ORDER BY package, version"""

    r = session.execute(query, params=dict(sources=sources))

    for i in r:
        print "%s_%s\n%s\n(%s)\n\n\n" % (i[0], i[1], i[2], i[3])

    session.rollback()

################################################################################

def end():
    accept_count = SummaryStats().accept_count
    accept_bytes = SummaryStats().accept_bytes

    if accept_count:
        sets = "set"
        if accept_count > 1:
            sets = "sets"
        sys.stderr.write("Accepted %d package %s, %s.\n" % (accept_count, sets, utils.size_type(int(accept_bytes))))
        Logger.log(["total",accept_count,accept_bytes])

    if not Options["No-Action"] and not Options["Trainee"]:
        Logger.close()

################################################################################

def main():
    global Options, Logger, Sections, Priorities

    cnf = Config()
    session = DBConn().session()

    Arguments = [('a',"automatic","Process-New::Options::Automatic"),
                 ('b',"no-binaries","Process-New::Options::No-Binaries"),
                 ('c',"comments","Process-New::Options::Comments"),
                 ('h',"help","Process-New::Options::Help"),
                 ('m',"manual-reject","Process-New::Options::Manual-Reject", "HasArg"),
                 ('t',"trainee","Process-New::Options::Trainee"),
                 ('q','queue','Process-New::Options::Queue', 'HasArg'),
                 ('n',"no-action","Process-New::Options::No-Action")]

    changes_files = apt_pkg.parse_commandline(cnf.Cnf,Arguments,sys.argv)

    for i in ["automatic", "no-binaries", "comments", "help", "manual-reject", "no-action", "version", "trainee"]:
        if not cnf.has_key("Process-New::Options::%s" % (i)):
            cnf["Process-New::Options::%s" % (i)] = ""

    queue_name = cnf.get('Process-New::Options::Queue', 'new')
    new_queue = session.query(PolicyQueue).filter_by(queue_name=queue_name).one()
    if len(changes_files) == 0:
        uploads = new_queue.uploads
    else:
        uploads = session.query(PolicyQueueUpload).filter_by(policy_queue=new_queue) \
            .join(DBChange).filter(DBChange.changesname.in_(changes_files)).all()

    Options = cnf.subtree("Process-New::Options")

    if Options["Help"]:
        usage()

    if not Options["No-Action"]:
        try:
            Logger = daklog.Logger("process-new")
        except CantOpenError as e:
            Options["Trainee"] = "True"

    Sections = Section_Completer(session)
    Priorities = Priority_Completer(session)
    readline.parse_and_bind("tab: complete")

    if len(uploads) > 1:
        sys.stderr.write("Sorting changes...\n")
        uploads.sort()

    if Options["Comments"]:
        show_new_comments(uploads, session)
    else:
        for upload in uploads:
            print "\n" + os.path.basename(upload.changes.changesname)

            do_pkg (upload, session)

    end()

################################################################################

if __name__ == '__main__':
    main()
