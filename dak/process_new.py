#!/usr/bin/env python
# vim:set et ts=4 sw=4:

# Handles NEW and BYHAND packages
# Copyright (C) 2001, 2002, 2003, 2004, 2005, 2006  James Troup <james@nocrew.org>

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

import copy, errno, os, readline, stat, sys, time
import apt_pkg, apt_inst
import examine_package
from daklib import database
from daklib import logging
from daklib import queue
from daklib import utils

# Globals
Cnf = None
Options = None
Upload = None
projectB = None
Logger = None

Priorities = None
Sections = None

reject_message = ""

################################################################################
################################################################################
################################################################################

def reject (str, prefix="Rejected: "):
    global reject_message
    if str:
        reject_message += prefix + str + "\n"

def recheck():
    global reject_message
    files = Upload.pkg.files
    reject_message = ""

    for f in files.keys():
        # The .orig.tar.gz can disappear out from under us is it's a
        # duplicate of one in the archive.
        if not files.has_key(f):
            continue
        # Check that the source still exists
        if files[f]["type"] == "deb":
            source_version = files[f]["source version"]
            source_package = files[f]["source package"]
            if not Upload.pkg.changes["architecture"].has_key("source") \
               and not Upload.source_exists(source_package, source_version, Upload.pkg.changes["distribution"].keys()):
                source_epochless_version = utils.re_no_epoch.sub('', source_version)
                dsc_filename = "%s_%s.dsc" % (source_package, source_epochless_version)
                found = 0
                for q in ["Accepted", "Embargoed", "Unembargoed"]:
                    if Cnf.has_key("Dir::Queue::%s" % (q)):
                        if os.path.exists(Cnf["Dir::Queue::%s" % (q)] + '/' + dsc_filename):
                            found = 1
                if not found:
                    reject("no source found for %s %s (%s)." % (source_package, source_version, f))

        # Version and file overwrite checks
        if files[f]["type"] == "deb":
            reject(Upload.check_binary_against_db(f), "")
        elif files[f]["type"] == "dsc":
            reject(Upload.check_source_against_db(f), "")
            (reject_msg, is_in_incoming) = Upload.check_dsc_against_db(f)
            reject(reject_msg, "")

    if reject_message.find("Rejected") != -1:
        answer = "XXX"
        if Options["No-Action"] or Options["Automatic"]:
            answer = 'S'

        print "REJECT\n" + reject_message,
        prompt = "[R]eject, Skip, Quit ?"

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.match(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

        if answer == 'R':
            Upload.do_reject(0, reject_message)
            os.unlink(Upload.pkg.changes_file[:-8]+".dak")
            return 0
        elif answer == 'S':
            return 0
        elif answer == 'Q':
            end()
            sys.exit(0)

    return 1

################################################################################

def indiv_sg_compare (a, b):
    """Sort by source name, source, version, 'have source', and
       finally by filename."""
    # Sort by source version
    q = apt_pkg.VersionCompare(a["version"], b["version"])
    if q:
        return -q

    # Sort by 'have source'
    a_has_source = a["architecture"].get("source")
    b_has_source = b["architecture"].get("source")
    if a_has_source and not b_has_source:
        return -1
    elif b_has_source and not a_has_source:
        return 1

    return cmp(a["filename"], b["filename"])

############################################################

def sg_compare (a, b):
    a = a[1]
    b = b[1]
    """Sort by have note, source already in database and time of oldest upload."""
    # Sort by have note
    a_note_state = a["note_state"]
    b_note_state = b["note_state"]
    if a_note_state < b_note_state:
        return -1
    elif a_note_state > b_note_state:
        return 1
    # Sort by source already in database (descending)
    source_in_database = cmp(a["source_in_database"], b["source_in_database"])
    if source_in_database:
        return -source_in_database

    # Sort by time of oldest upload
    return cmp(a["oldest"], b["oldest"])

def sort_changes(changes_files):
    """Sort into source groups, then sort each source group by version,
    have source, filename.  Finally, sort the source groups by have
    note, time of oldest upload of each source upload."""
    if len(changes_files) == 1:
        return changes_files

    sorted_list = []
    cache = {}
    # Read in all the .changes files
    for filename in changes_files:
        try:
            Upload.pkg.changes_file = filename
            Upload.init_vars()
            Upload.update_vars()
            cache[filename] = copy.copy(Upload.pkg.changes)
            cache[filename]["filename"] = filename
        except:
            sorted_list.append(filename)
            break
    # Divide the .changes into per-source groups
    per_source = {}
    for filename in cache.keys():
        source = cache[filename]["source"]
        if not per_source.has_key(source):
            per_source[source] = {}
            per_source[source]["list"] = []
        per_source[source]["list"].append(cache[filename])
    # Determine oldest time and have note status for each source group
    for source in per_source.keys():
        q = projectB.query("SELECT 1 FROM source WHERE source = '%s'" % source)
        ql = q.getresult()
        per_source[source]["source_in_database"] = len(ql)>0
        source_list = per_source[source]["list"]
        first = source_list[0]
        oldest = os.stat(first["filename"])[stat.ST_MTIME]
        have_note = 0
        for d in per_source[source]["list"]:
            mtime = os.stat(d["filename"])[stat.ST_MTIME]
            if mtime < oldest:
                oldest = mtime
            have_note += (d.has_key("process-new note"))
        per_source[source]["oldest"] = oldest
        if not have_note:
            per_source[source]["note_state"] = 0; # none
        elif have_note < len(source_list):
            per_source[source]["note_state"] = 1; # some
        else:
            per_source[source]["note_state"] = 2; # all
        per_source[source]["list"].sort(indiv_sg_compare)
    per_source_items = per_source.items()
    per_source_items.sort(sg_compare)
    for i in per_source_items:
        for j in i[1]["list"]:
            sorted_list.append(j["filename"])
    return sorted_list

################################################################################

class Section_Completer:
    def __init__ (self):
        self.sections = []
        q = projectB.query("SELECT section FROM section")
        for i in q.getresult():
            self.sections.append(i[0])

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
    def __init__ (self):
        self.priorities = []
        q = projectB.query("SELECT priority FROM priority")
        for i in q.getresult():
            self.priorities.append(i[0])

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

def print_new (new, indexed, file=sys.stdout):
    queue.check_valid(new)
    broken = 0
    index = 0
    for pkg in new.keys():
        index += 1
        section = new[pkg]["section"]
        priority = new[pkg]["priority"]
        if new[pkg]["section id"] == -1:
            section += "[!]"
            broken = 1
        if new[pkg]["priority id"] == -1:
            priority += "[!]"
            broken = 1
        if indexed:
            line = "(%s): %-20s %-20s %-20s" % (index, pkg, priority, section)
        else:
            line = "%-20s %-20s %-20s" % (pkg, priority, section)
        line = line.strip()+'\n'
        file.write(line)
    note = Upload.pkg.changes.get("process-new note")
    if note:
        print "*"*75
        print note
        print "*"*75
    return broken, note

################################################################################

def index_range (index):
    if index == 1:
        return "1"
    else:
        return "1-%s" % (index)

################################################################################
################################################################################

def edit_new (new):
    # Write the current data to a temporary file
    temp_filename = utils.temp_filename()
    temp_file = utils.open_file(temp_filename, 'w')
    print_new (new, 0, temp_file)
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
    # Parse the new data
    for line in lines:
        line = line.strip()
        if line == "":
            continue
        s = line.split()
        # Pad the list if necessary
        s[len(s):3] = [None] * (3-len(s))
        (pkg, priority, section) = s[:3]
        if not new.has_key(pkg):
            utils.warn("Ignoring unknown package '%s'" % (pkg))
        else:
            # Strip off any invalid markers, print_new will readd them.
            if section.endswith("[!]"):
                section = section[:-3]
            if priority.endswith("[!]"):
                priority = priority[:-3]
            for f in new[pkg]["files"]:
                Upload.pkg.files[f]["section"] = section
                Upload.pkg.files[f]["priority"] = priority
            new[pkg]["section"] = section
            new[pkg]["priority"] = priority

################################################################################

def edit_index (new, index):
    priority = new[index]["priority"]
    section = new[index]["section"]
    ftype = new[index]["type"]
    done = 0
    while not done:
        print "\t".join([index, priority, section])

        answer = "XXX"
        if ftype != "dsc":
            prompt = "[B]oth, Priority, Section, Done ? "
        else:
            prompt = "[S]ection, Done ? "
        edit_priority = edit_section = 0

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.match(prompt)
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

    for f in new[index]["files"]:
        Upload.pkg.files[f]["section"] = section
        Upload.pkg.files[f]["priority"] = priority
    new[index]["priority"] = priority
    new[index]["section"] = section
    return new

################################################################################

def edit_overrides (new):
    print
    done = 0
    while not done:
        print_new (new, 1)
        new_index = {}
        index = 0
        for i in new.keys():
            index += 1
            new_index[index] = i

        prompt = "(%s) edit override <n>, Editor, Done ? " % (index_range(index))

        got_answer = 0
        while not got_answer:
            answer = utils.our_raw_input(prompt)
            if not answer.isdigit():
                answer = answer[:1].upper()
            if answer == "E" or answer == "D":
                got_answer = 1
            elif queue.re_isanum.match (answer):
                answer = int(answer)
                if (answer < 1) or (answer > index):
                    print "%s is not a valid index (%s).  Please retry." % (answer, index_range(index))
                else:
                    got_answer = 1

        if answer == 'E':
            edit_new(new)
        elif answer == 'D':
            done = 1
        else:
            edit_index (new, new_index[answer])

    return new

################################################################################

def edit_note(note):
    # Write the current data to a temporary file
    temp_filename = utils.temp_filename()
    temp_file = utils.open_file(temp_filename, 'w')
    temp_file.write(note)
    temp_file.close()
    editor = os.environ.get("EDITOR","vi")
    answer = 'E'
    while answer == 'E':
        os.system("%s %s" % (editor, temp_filename))
        temp_file = utils.open_file(temp_filename)
        note = temp_file.read().rstrip()
        temp_file.close()
        print "Note:"
        print utils.prefix_multi_line_string(note,"  ")
        prompt = "[D]one, Edit, Abandon, Quit ?"
        answer = "XXX"
        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()
    os.unlink(temp_filename)
    if answer == 'A':
        return
    elif answer == 'Q':
        end()
        sys.exit(0)
    Upload.pkg.changes["process-new note"] = note
    Upload.dump_vars(Cnf["Dir::Queue::New"])

################################################################################

def check_pkg ():
    try:
        less_fd = os.popen("less -R -", 'w', 0)
        stdout_fd = sys.stdout
        try:
            sys.stdout = less_fd
            examine_package.display_changes(Upload.pkg.changes_file)
            files = Upload.pkg.files
            for f in files.keys():
                if files[f].has_key("new"):
                    ftype = files[f]["type"]
                    if ftype == "deb":
                        examine_package.check_deb(f)
                    elif ftype == "dsc":
                        examine_package.check_dsc(f)
        finally:
            sys.stdout = stdout_fd
    except IOError, e:
        if e.errno == errno.EPIPE:
            utils.warn("[examine_package] Caught EPIPE; skipping.")
            pass
        else:
            raise
    except KeyboardInterrupt:
        utils.warn("[examine_package] Caught C-c; skipping.")
        pass

################################################################################

## FIXME: horribly Debian specific

def do_bxa_notification():
    files = Upload.pkg.files
    summary = ""
    for f in files.keys():
        if files[f]["type"] == "deb":
            control = apt_pkg.ParseSection(apt_inst.debExtractControl(utils.open_file(f)))
            summary += "\n"
            summary += "Package: %s\n" % (control.Find("Package"))
            summary += "Description: %s\n" % (control.Find("Description"))
    Upload.Subst["__BINARY_DESCRIPTIONS__"] = summary
    bxa_mail = utils.TemplateSubst(Upload.Subst,Cnf["Dir::Templates"]+"/process-new.bxa_notification")
    utils.send_mail(bxa_mail)

################################################################################

def add_overrides (new):
    changes = Upload.pkg.changes
    files = Upload.pkg.files

    projectB.query("BEGIN WORK")
    for suite in changes["suite"].keys():
        suite_id = database.get_suite_id(suite)
        for pkg in new.keys():
            component_id = database.get_component_id(new[pkg]["component"])
            type_id = database.get_override_type_id(new[pkg]["type"])
            priority_id = new[pkg]["priority id"]
            section_id = new[pkg]["section id"]
            projectB.query("INSERT INTO override (suite, component, type, package, priority, section, maintainer) VALUES (%s, %s, %s, '%s', %s, %s, '')" % (suite_id, component_id, type_id, pkg, priority_id, section_id))
            for f in new[pkg]["files"]:
                if files[f].has_key("new"):
                    del files[f]["new"]
            del new[pkg]

    projectB.query("COMMIT WORK")

    if Cnf.FindB("Dinstall::BXANotify"):
        do_bxa_notification()

################################################################################

def prod_maintainer ():
    # Here we prepare an editor and get them ready to prod...
    temp_filename = utils.temp_filename()
    editor = os.environ.get("EDITOR","vi")
    answer = 'E'
    while answer == 'E':
        os.system("%s %s" % (editor, temp_filename))
        f = utils.open_file(temp_filename)
        prod_message = "".join(f.readlines())
        f.close()
        print "Prod message:"
        print utils.prefix_multi_line_string(prod_message,"  ",include_blank_lines=1)
        prompt = "[P]rod, Edit, Abandon, Quit ?"
        answer = "XXX"
        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()
        os.unlink(temp_filename)
        if answer == 'A':
            return
        elif answer == 'Q':
            end()
            sys.exit(0)
    # Otherwise, do the proding...
    user_email_address = utils.whoami() + " <%s>" % (
        Cnf["Dinstall::MyAdminAddress"])

    Subst = Upload.Subst

    Subst["__FROM_ADDRESS__"] = user_email_address
    Subst["__PROD_MESSAGE__"] = prod_message
    Subst["__CC__"] = "Cc: " + Cnf["Dinstall::MyEmailAddress"]

    prod_mail_message = utils.TemplateSubst(
        Subst,Cnf["Dir::Templates"]+"/process-new.prod")

    # Send the prod mail if appropriate
    if not Cnf["Dinstall::Options::No-Mail"]:
        utils.send_mail(prod_mail_message)

    print "Sent proding message"

################################################################################

def do_new():
    print "NEW\n"
    files = Upload.pkg.files
    changes = Upload.pkg.changes

    # Make a copy of distribution we can happily trample on
    changes["suite"] = copy.copy(changes["distribution"])

    # Fix up the list of target suites
    for suite in changes["suite"].keys():
        override = Cnf.Find("Suite::%s::OverrideSuite" % (suite))
        if override:
            (olderr, newerr) = (database.get_suite_id(suite) == -1,
              database.get_suite_id(override) == -1)
            if olderr or newerr:
                (oinv, newinv) = ("", "")
                if olderr: oinv = "invalid "
                if newerr: ninv = "invalid "
                print "warning: overriding %ssuite %s to %ssuite %s" % (
                        oinv, suite, ninv, override)
            del changes["suite"][suite]
            changes["suite"][override] = 1
    # Validate suites
    for suite in changes["suite"].keys():
        suite_id = database.get_suite_id(suite)
        if suite_id == -1:
            utils.fubar("%s has invalid suite '%s' (possibly overriden).  say wha?" % (changes, suite))

    # The main NEW processing loop
    done = 0
    while not done:
        # Find out what's new
        new = queue.determine_new(changes, files, projectB)

        if not new:
            break

        answer = "XXX"
        if Options["No-Action"] or Options["Automatic"]:
            answer = 'S'

        (broken, note) = print_new(new, 0)
        prompt = ""

        if not broken and not note:
            prompt = "Add overrides, "
        if broken:
            print "W: [!] marked entries must be fixed before package can be processed."
        if note:
            print "W: note must be removed before package can be processed."
            prompt += "Remove note, "

        prompt += "Edit overrides, Check, Manual reject, Note edit, Prod, [S]kip, Quit ?"

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

        if answer == 'A':
            done = add_overrides (new)
        elif answer == 'C':
            check_pkg()
        elif answer == 'E':
            new = edit_overrides (new)
        elif answer == 'M':
            aborted = Upload.do_reject(1, Options["Manual-Reject"])
            if not aborted:
                os.unlink(Upload.pkg.changes_file[:-8]+".dak")
                done = 1
        elif answer == 'N':
            edit_note(changes.get("process-new note", ""))
        elif answer == 'P':
            prod_maintainer()
        elif answer == 'R':
            confirm = utils.our_raw_input("Really clear note (y/N)? ").lower()
            if confirm == "y":
                del changes["process-new note"]
        elif answer == 'S':
            done = 1
        elif answer == 'Q':
            end()
            sys.exit(0)

################################################################################
################################################################################
################################################################################

def usage (exit_code=0):
    print """Usage: dak process-new [OPTION]... [CHANGES]...
  -a, --automatic           automatic run
  -h, --help                show this help and exit.
  -C, --comments-dir=DIR    use DIR as comments-dir, for [o-]p-u-new
  -m, --manual-reject=MSG   manual reject with `msg'
  -n, --no-action           don't do anything
  -V, --version             display the version number and exit"""
    sys.exit(exit_code)

################################################################################

def init():
    global Cnf, Options, Logger, Upload, projectB, Sections, Priorities

    Cnf = utils.get_conf()

    Arguments = [('a',"automatic","Process-New::Options::Automatic"),
                 ('h',"help","Process-New::Options::Help"),
                 ('C',"comments-dir","Process-New::Options::Comments-Dir", "HasArg"),
                 ('m',"manual-reject","Process-New::Options::Manual-Reject", "HasArg"),
                 ('n',"no-action","Process-New::Options::No-Action")]

    for i in ["automatic", "help", "manual-reject", "no-action", "version", "comments-dir"]:
        if not Cnf.has_key("Process-New::Options::%s" % (i)):
            Cnf["Process-New::Options::%s" % (i)] = ""

    changes_files = apt_pkg.ParseCommandLine(Cnf,Arguments,sys.argv)
    Options = Cnf.SubTree("Process-New::Options")

    if Options["Help"]:
        usage()

    Upload = queue.Upload(Cnf)

    if not Options["No-Action"]:
        Logger = Upload.Logger = logging.Logger(Cnf, "process-new")

    projectB = Upload.projectB

    Sections = Section_Completer()
    Priorities = Priority_Completer()
    readline.parse_and_bind("tab: complete")

    return changes_files

################################################################################

def do_byhand():
    done = 0
    while not done:
        files = Upload.pkg.files
        will_install = 1
        byhand = []

        for f in files.keys():
            if files[f]["type"] == "byhand":
                if os.path.exists(f):
                    print "W: %s still present; please process byhand components and try again." % (f)
                    will_install = 0
                else:
                    byhand.append(f)

        answer = "XXXX"
        if Options["No-Action"]:
            answer = "S"
        if will_install:
            if Options["Automatic"] and not Options["No-Action"]:
                answer = 'A'
            prompt = "[A]ccept, Manual reject, Skip, Quit ?"
        else:
            prompt = "Manual reject, [S]kip, Quit ?"

        while prompt.find(answer) == -1:
            answer = utils.our_raw_input(prompt)
            m = queue.re_default_answer.search(prompt)
            if answer == "":
                answer = m.group(1)
            answer = answer[:1].upper()

        if answer == 'A':
            done = 1
            for f in byhand:
                del files[f]
        elif answer == 'M':
            Upload.do_reject(1, Options["Manual-Reject"])
            os.unlink(Upload.pkg.changes_file[:-8]+".dak")
            done = 1
        elif answer == 'S':
            done = 1
        elif answer == 'Q':
            end()
            sys.exit(0)

################################################################################

def get_accept_lock():
    retry = 0
    while retry < 10:
        try:
            os.open(Cnf["Process-New::AcceptedLockFile"], os.O_RDONLY | os.O_CREAT | os.O_EXCL)
            retry = 10
        except OSError, e:
            if e.errno == errno.EACCES or e.errno == errno.EEXIST:
                retry += 1
                if (retry >= 10):
                    utils.fubar("Couldn't obtain lock; assuming 'dak process-unchecked' is already running.")
                else:
                    print("Unable to get accepted lock (try %d of 10)" % retry)
                time.sleep(60)
            else:
                raise

def move_to_dir (dest, perms=0660, changesperms=0664):
    utils.move (Upload.pkg.changes_file, dest, perms=changesperms)
    file_keys = Upload.pkg.files.keys()
    for f in file_keys:
        utils.move (f, dest, perms=perms)

def is_source_in_queue_dir(qdir):
    entries = [ x for x in os.listdir(qdir) if x.startswith(Upload.pkg.changes["source"])
                and x.endswith(".changes") ]
    for entry in entries:
        # read the .dak
        u = queue.Upload(Cnf)
        u.pkg.changes_file = os.path.join(qdir, entry)
        u.update_vars()
        if not u.pkg.changes["architecture"].has_key("source"):
            # another binary upload, ignore
            continue
        if Upload.pkg.changes["version"] != u.pkg.changes["version"]:
            # another version, ignore
            continue
        # found it!
        return True
    return False

def move_to_holding(suite, queue_dir):
    print "Moving to %s holding area." % (suite.upper(),)
    if Options["No-Action"]:
    	return
    Logger.log(["Moving to %s" % (suite,), Upload.pkg.changes_file])
    Upload.dump_vars(queue_dir)
    move_to_dir(queue_dir)
    os.unlink(Upload.pkg.changes_file[:-8]+".dak")

def _accept():
    if Options["No-Action"]:
        return
    (summary, short_summary) = Upload.build_summaries()
    Upload.accept(summary, short_summary)
    os.unlink(Upload.pkg.changes_file[:-8]+".dak")

def do_accept_stableupdate(suite, q):
    queue_dir = Cnf["Dir::Queue::%s" % (q,)]
    if not Upload.pkg.changes["architecture"].has_key("source"):
        # It is not a sourceful upload.  So its source may be either in p-u
        # holding, in new, in accepted or already installed.
        if is_source_in_queue_dir(queue_dir):
            # It's in p-u holding, so move it there.
            print "Binary-only upload, source in %s." % (q,)
            move_to_holding(suite, queue_dir)
        elif Upload.source_exists(Upload.pkg.changes["source"],
                Upload.pkg.changes["version"]):
            # dak tells us that there is source available.  At time of
            # writing this means that it is installed, so put it into
            # accepted.
            print "Binary-only upload, source installed."
            _accept()
        elif is_source_in_queue_dir(Cnf["Dir::Queue::Accepted"]):
            # The source is in accepted, the binary cleared NEW: accept it.
            print "Binary-only upload, source in accepted."
            _accept()
        elif is_source_in_queue_dir(Cnf["Dir::Queue::New"]):
            # It's in NEW.  We expect the source to land in p-u holding
            # pretty soon.
            print "Binary-only upload, source in new."
            move_to_holding(suite, queue_dir)
        else:
            # No case applicable.  Bail out.  Return will cause the upload
            # to be skipped.
            print "ERROR"
            print "Stable update failed.  Source not found."
            return
    else:
        # We are handling a sourceful upload.  Move to accepted if currently
        # in p-u holding and to p-u holding otherwise.
        if is_source_in_queue_dir(queue_dir):
            print "Sourceful upload in %s, accepting." % (q,)
            _accept()
        else:
            move_to_holding(suite, queue_dir)

def do_accept():
    print "ACCEPT"
    if not Options["No-Action"]:
        get_accept_lock()
        (summary, short_summary) = Upload.build_summaries()
    try:
        if Cnf.FindB("Dinstall::SecurityQueueHandling"):
            Upload.dump_vars(Cnf["Dir::Queue::Embargoed"])
            move_to_dir(Cnf["Dir::Queue::Embargoed"])
            Upload.queue_build("embargoed", Cnf["Dir::Queue::Embargoed"])
            # Check for override disparities
            Upload.Subst["__SUMMARY__"] = summary
        else:
            # Stable updates need to be copied to proposed-updates holding
            # area instead of accepted.  Sourceful uploads need to go
            # to it directly, binaries only if the source has not yet been
            # accepted into p-u.
            for suite, q in [("proposed-updates", "ProposedUpdates"),
                    ("oldstable-proposed-updates", "OldProposedUpdates")]:
                if not Upload.pkg.changes["distribution"].has_key(suite):
                    continue
                return do_accept_stableupdate(suite, q)
            # Just a normal upload, accept it...
            _accept()
    finally:
        if not Options["No-Action"]:
            os.unlink(Cnf["Process-New::AcceptedLockFile"])

def check_status(files):
    new = byhand = 0
    for f in files.keys():
        if files[f]["type"] == "byhand":
            byhand = 1
        elif files[f].has_key("new"):
            new = 1
    return (new, byhand)

def do_pkg(changes_file):
    Upload.pkg.changes_file = changes_file
    Upload.init_vars()
    Upload.update_vars()
    Upload.update_subst()
    files = Upload.pkg.files

    if not recheck():
        return

    (new, byhand) = check_status(files)
    if new or byhand:
        if new:
            do_new()
        if byhand:
            do_byhand()
        (new, byhand) = check_status(files)

    if not new and not byhand:
        do_accept()

################################################################################

def end():
    accept_count = Upload.accept_count
    accept_bytes = Upload.accept_bytes

    if accept_count:
        sets = "set"
        if accept_count > 1:
            sets = "sets"
        sys.stderr.write("Accepted %d package %s, %s.\n" % (accept_count, sets, utils.size_type(int(accept_bytes))))
        Logger.log(["total",accept_count,accept_bytes])

    if not Options["No-Action"]:
        Logger.close()

################################################################################

def do_comments(dir, opref, npref, line, fn):
    for comm in [ x for x in os.listdir(dir) if x.startswith(opref) ]:
        lines = open("%s/%s" % (dir, comm)).readlines()
        if len(lines) == 0 or lines[0] != line + "\n": continue
        changes_files = [ x for x in os.listdir(".") if x.startswith(comm[7:]+"_")
                                and x.endswith(".changes") ]
        changes_files = sort_changes(changes_files)
        for f in changes_files:
            f = utils.validate_changes_file_arg(f, 0)
            if not f: continue
            print "\n" + f
            fn(f, "".join(lines[1:]))

        if opref != npref and not Options["No-Action"]:
            newcomm = npref + comm[len(opref):]
            os.rename("%s/%s" % (dir, comm), "%s/%s" % (dir, newcomm))

################################################################################

def comment_accept(changes_file, comments):
    Upload.pkg.changes_file = changes_file
    Upload.init_vars()
    Upload.update_vars()
    Upload.update_subst()
    files = Upload.pkg.files

    if not recheck():
        return # dak wants to REJECT, crap

    (new, byhand) = check_status(files)
    if not new and not byhand:
        do_accept()

################################################################################

def comment_reject(changes_file, comments):
    Upload.pkg.changes_file = changes_file
    Upload.init_vars()
    Upload.update_vars()
    Upload.update_subst()

    if not recheck():
        pass # dak has its own reasons to reject as well, which is fine

    reject(comments)
    print "REJECT\n" + reject_message,
    if not Options["No-Action"]:
        Upload.do_reject(0, reject_message)
        os.unlink(Upload.pkg.changes_file[:-8]+".dak")

################################################################################

def main():
    changes_files = init()
    if len(changes_files) > 50:
        sys.stderr.write("Sorting changes...\n")
    changes_files = sort_changes(changes_files)

    # Kill me now? **FIXME**
    Cnf["Dinstall::Options::No-Mail"] = ""
    bcc = "X-DAK: dak process-new\nX-Katie: lisa $Revision: 1.31 $"
    if Cnf.has_key("Dinstall::Bcc"):
        Upload.Subst["__BCC__"] = bcc + "\nBcc: %s" % (Cnf["Dinstall::Bcc"])
    else:
        Upload.Subst["__BCC__"] = bcc

    commentsdir = Cnf.get("Process-New::Options::Comments-Dir","")
    if commentsdir:
        if changes_files != []:
            sys.stderr.write("Can't specify any changes files if working with comments-dir")
            sys.exit(1)
        do_comments(commentsdir, "ACCEPT.", "ACCEPTED.", "OK", comment_accept)
        do_comments(commentsdir, "REJECT.", "REJECTED.", "NOTOK", comment_reject)
    else:
        for changes_file in changes_files:
            changes_file = utils.validate_changes_file_arg(changes_file, 0)
            if not changes_file:
                continue
            print "\n" + changes_file
            do_pkg (changes_file)

    end()

################################################################################

if __name__ == '__main__':
    main()
