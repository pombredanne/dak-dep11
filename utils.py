#!/usr/bin/env python

# Utility functions
# Copyright (C) 2000, 2001, 2002  James Troup <james@nocrew.org>
# $Id: utils.py,v 1.52 2002-11-22 04:06:34 troup Exp $

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

import commands, os, pwd, re, socket, shutil, string, sys, tempfile, traceback;
import apt_pkg;
import db_access;

################################################################################

re_comments = re.compile(r"\#.*")
re_no_epoch = re.compile(r"^\d*\:")
re_no_revision = re.compile(r"\-[^-]*$")
re_arch_from_filename = re.compile(r"/binary-[^/]+/")
re_extract_src_version = re.compile (r"(\S+)\s*\((.*)\)")
re_isadeb = re.compile (r"(.+?)_(.+?)_(.+)\.u?deb$");
re_issource = re.compile (r"(.+)_(.+?)\.(orig\.tar\.gz|diff\.gz|tar\.gz|dsc)$");

re_single_line_field = re.compile(r"^(\S*)\s*:\s*(.*)");
re_multi_line_field = re.compile(r"^\s(.*)");
re_taint_free = re.compile(r"^[-+~\.\w]+$");

re_parse_maintainer = re.compile(r"^\s*(\S.*\S)\s*\<([^\> \t]+)\>");

changes_parse_error_exc = "Can't parse line in .changes file";
invalid_dsc_format_exc = "Invalid .dsc file";
nk_format_exc = "Unknown Format: in .changes file";
no_files_exc = "No Files: field in .dsc file.";
cant_open_exc = "Can't read file.";
unknown_hostname_exc = "Unknown hostname";
cant_overwrite_exc = "Permission denied; can't overwrite existent file."
file_exists_exc = "Destination file exists";
send_mail_invalid_args_exc = "Both arguments are non-null.";
sendmail_failed_exc = "Sendmail invocation failed";
tried_too_hard_exc = "Tried too hard to find a free filename.";

default_config = "/etc/katie/katie.conf";
default_apt_config = "/etc/katie/apt.conf";

######################################################################################

def open_file(filename, mode='r'):
    try:
	f = open(filename, mode);
    except IOError:
        raise cant_open_exc, filename
    return f

######################################################################################

def our_raw_input(prompt=""):
    if prompt:
        sys.stdout.write(prompt);
    sys.stdout.flush();
    try:
        ret = raw_input();
        return ret
    except EOFError:
        sys.stderr.write('\nUser interrupt (^D).\n');
        raise SystemExit;

######################################################################################

def str_isnum (s):
    for c in s:
        if c not in string.digits:
            return 0;
    return 1;

######################################################################################

def extract_component_from_section(section):
    component = "";

    if section.find('/') != -1:
        component = section.split('/')[0];
    if component.lower() == "non-us" and section.count('/') > 0:
        s = component + '/' + section.split('/')[1];
        if Cnf.has_key("Component::%s" % s): # Avoid e.g. non-US/libs
            component = s;

    if section.lower() == "non-us":
        component = "non-US/main";

    # non-US prefix is case insensitive
    if component.lower()[:6] == "non-us":
        component = "non-US"+component[6:];

    # Expand default component
    if component == "":
        if Cnf.has_key("Component::%s" % section):
            component = section;
        else:
            component = "main";
    elif component == "non-US":
        component = "non-US/main";

    return (section, component);

######################################################################################

# dsc_whitespace_rules turns on strict format checking to avoid
# allowing in source packages which are unextracable by the
# inappropriately fragile dpkg-source.
#
# The rules are:
#
#
# o The PGP header consists of "-----BEGIN PGP SIGNED MESSAGE-----"
#   followed by any PGP header data and must end with a blank line.
#
# o The data section must end with a blank line and must be followed by
#   "-----BEGIN PGP SIGNATURE-----".

def parse_changes(filename, dsc_whitespace_rules=0):
    changes_in = open_file(filename);
    error = "";
    changes = {};
    lines = changes_in.readlines();

    if not lines:
	raise changes_parse_error_exc, "[Empty changes file]";

    # Reindex by line number so we can easily verify the format of
    # .dsc files...
    index = 0;
    indexed_lines = {};
    for line in lines:
        index += 1;
        indexed_lines[index] = line[:-1];

    inside_signature = 0;

    indices = indexed_lines.keys()
    index = 0;
    first = -1;
    while index < max(indices):
        index += 1;
        line = indexed_lines[index];
        if line == "":
            if dsc_whitespace_rules:
                index += 1;
                if index > max(indices):
                    raise invalid_dsc_format_exc, index;
                line = indexed_lines[index];
                if not line.startswith("-----BEGIN PGP SIGNATURE"):
                    raise invalid_dsc_format_exc, index;
                inside_signature = 0;
                break;
        if line.startswith("-----BEGIN PGP SIGNATURE"):
            break;
        if line.startswith("-----BEGIN PGP SIGNED MESSAGE"):
            if dsc_whitespace_rules:
                inside_signature = 1;
                while index < max(indices) and line != "":
                    index += 1;
                    line = indexed_lines[index];
            continue;
        slf = re_single_line_field.match(line);
        if slf:
            field = slf.groups()[0].lower();
            changes[field] = slf.groups()[1];
	    first = 1;
            continue;
        if line == " .":
            changes[field] += '\n';
            continue;
        mlf = re_multi_line_field.match(line);
        if mlf:
            if first == -1:
                raise changes_parse_error_exc, "'%s'\n [Multi-line field continuing on from nothing?]" % (line);
            if first == 1 and changes[field] != "":
                changes[field] += '\n';
            first = 0;
	    changes[field] += mlf.groups()[0] + '\n';
            continue;
	error += line;

    if dsc_whitespace_rules and inside_signature:
        raise invalid_dsc_format_exc, index;

    changes_in.close();
    changes["filecontents"] = "".join(lines);

    if error != "":
	raise changes_parse_error_exc, error;

    return changes;

######################################################################################

# Dropped support for 1.4 and ``buggy dchanges 3.4'' (?!) compared to di.pl

def build_file_list(changes, is_a_dsc=0):
    files = {}
    format = changes.get("format", "")
    if format != "":
	format = float(format)
    if not is_a_dsc and (format < 1.5 or format > 2.0):
	raise nk_format_exc, format;

    # No really, this has happened.  Think 0 length .dsc file.
    if not changes.has_key("files"):
	raise no_files_exc

    for i in changes["files"].split("\n"):
        if i == "":
            break
        s = i.split();
        section = priority = "";
        try:
            if is_a_dsc:
                (md5, size, name) = s
            else:
                (md5, size, section, priority, name) = s
        except ValueError:
            raise changes_parse_error_exc, i

        if section == "": section = "-"
        if priority == "": priority = "-"

        (section, component) = extract_component_from_section(section);

        files[name] = { "md5sum" : md5,
                        "size" : size,
                        "section": section,
                        "priority": priority,
                        "component": component }

    return files

######################################################################################

# Fix the `Maintainer:' field to be an RFC822 compatible address.
# cf. Packaging Manual (4.2.4)
#
# 06:28|<Culus> 'The standard sucks, but my tool is supposed to
#                interoperate with it. I know - I'll fix the suckage
#                and make things incompatible!'

def fix_maintainer (maintainer):
    m = re_parse_maintainer.match(maintainer);
    rfc822 = maintainer
    name = ""
    email = ""
    if m != None and len(m.groups()) == 2:
        name = m.group(1)
        email = m.group(2)
        if name.find(',') != -1 or name.find('.') != -1:
            rfc822 = re_parse_maintainer.sub(r"\2 (\1)", maintainer)
    return (rfc822, name, email)

######################################################################################

# sendmail wrapper, takes _either_ a message string or a file as arguments
def send_mail (message, filename):
	# Sanity check arguments
	if message != "" and filename != "":
            raise send_mail_invalid_args_exc;

	# If we've been passed a string dump it into a temporary file
	if message != "":
            filename = tempfile.mktemp();
            fd = os.open(filename, os.O_RDWR|os.O_CREAT|os.O_EXCL, 0700);
            os.write (fd, message);
            os.close (fd);

	# Invoke sendmail
	(result, output) = commands.getstatusoutput("%s < %s" % (Cnf["Dinstall::SendmailCommand"], filename));
	if (result != 0):
            raise sendmail_failed_exc, output;

	# Clean up any temporary files
	if message !="":
            os.unlink (filename);

######################################################################################

def poolify (source, component):
    if component != "":
	component += '/';
    # FIXME: this is nasty
    component = component.lower().replace('non-us/', 'non-US/');
    if source[:3] == "lib":
	return component + source[:4] + '/' + source + '/'
    else:
	return component + source[:1] + '/' + source + '/'

######################################################################################

def move (src, dest, overwrite = 0, perms = 0664):
    if os.path.exists(dest) and os.path.isdir(dest):
	dest_dir = dest;
    else:
	dest_dir = os.path.dirname(dest);
    if not os.path.exists(dest_dir):
	umask = os.umask(00000);
	os.makedirs(dest_dir, 02775);
	os.umask(umask);
    #print "Moving %s to %s..." % (src, dest);
    if os.path.exists(dest) and os.path.isdir(dest):
	dest += '/' + os.path.basename(src);
    # Don't overwrite unless forced to
    if os.path.exists(dest):
        if not overwrite:
            raise file_exists_exc;
        else:
            if not os.access(dest, os.W_OK):
                raise cant_overwrite_exc
    shutil.copy2(src, dest);
    os.chmod(dest, perms);
    os.unlink(src);

def copy (src, dest, overwrite = 0, perms = 0664):
    if os.path.exists(dest) and os.path.isdir(dest):
	dest_dir = dest;
    else:
	dest_dir = os.path.dirname(dest);
    if not os.path.exists(dest_dir):
	umask = os.umask(00000);
	os.makedirs(dest_dir, 02775);
	os.umask(umask);
    #print "Copying %s to %s..." % (src, dest);
    if os.path.exists(dest) and os.path.isdir(dest):
	dest += '/' + os.path.basename(src);
    # Don't overwrite unless forced to
    if os.path.exists(dest):
        if not overwrite:
            raise file_exists_exc
        else:
            if not os.access(dest, os.W_OK):
                raise cant_overwrite_exc
    shutil.copy2(src, dest);
    os.chmod(dest, perms);

######################################################################################

def where_am_i ():
    res = socket.gethostbyaddr(socket.gethostname());
    database_hostname = Cnf.get("Config::" + res[0] + "::DatabaseHostname");
    if database_hostname:
	return database_hostname;
    else:
        return res[0];

def which_conf_file ():
    res = socket.gethostbyaddr(socket.gethostname());
    if Cnf.get("Config::" + res[0] + "::KatieConfig"):
	return Cnf["Config::" + res[0] + "::KatieConfig"]
    else:
	return default_config;

def which_apt_conf_file ():
    res = socket.gethostbyaddr(socket.gethostname());
    if Cnf.get("Config::" + res[0] + "::AptConfig"):
	return Cnf["Config::" + res[0] + "::AptConfig"]
    else:
	return default_apt_config;

######################################################################################

# Escape characters which have meaning to SQL's regex comparison operator ('~')
# (woefully incomplete)

def regex_safe (s):
    s = s.replace('+', '\\\\+');
    s = s.replace('.', '\\\\.');
    return s

######################################################################################

# Perform a substition of template
def TemplateSubst(map, filename):
    file = open_file(filename);
    template = file.read();
    for x in map.keys():
        template = template.replace(x,map[x]);
    file.close();
    return template;

######################################################################################

def fubar(msg, exit_code=1):
    sys.stderr.write("E: %s\n" % (msg));
    sys.exit(exit_code);

def warn(msg):
    sys.stderr.write("W: %s\n" % (msg));

######################################################################################

# Returns the user name with a laughable attempt at rfc822 conformancy
# (read: removing stray periods).
def whoami ():
    return pwd.getpwuid(os.getuid())[4].split(',')[0].replace('.', '');

######################################################################################

def size_type (c):
    t  = " b";
    if c > 10000:
        c = c / 1000;
        t = " Kb";
    if c > 10000:
        c = c / 1000;
        t = " Mb";
    return ("%d%s" % (c, t))

################################################################################

def cc_fix_changes (changes):
    o = changes.get("architecture", "")
    if o != "":
        del changes["architecture"]
    changes["architecture"] = {}
    for j in o.split():
        changes["architecture"][j] = 1

# Sort by source name, source version, 'have source', and then by filename
def changes_compare (a, b):
    try:
        a_changes = parse_changes(a);
    except:
        return -1;

    try:
        b_changes = parse_changes(b);
    except:
        return 1;

    cc_fix_changes (a_changes);
    cc_fix_changes (b_changes);

    # Sort by source name
    a_source = a_changes.get("source");
    b_source = b_changes.get("source");
    q = cmp (a_source, b_source);
    if q:
        return q;

    # Sort by source version
    a_version = a_changes.get("version");
    b_version = b_changes.get("version");
    q = apt_pkg.VersionCompare(a_version, b_version);
    if q:
        return q;

    # Sort by 'have source'
    a_has_source = a_changes["architecture"].get("source");
    b_has_source = b_changes["architecture"].get("source");
    if a_has_source and not b_has_source:
        return -1;
    elif b_has_source and not a_has_source:
        return 1;

    # Fall back to sort by filename
    return cmp(a, b);

################################################################################

def find_next_free (dest, too_many=100):
    extra = 0;
    orig_dest = dest;
    while os.path.exists(dest) and extra < too_many:
        dest = orig_dest + '.' + repr(extra);
        extra += 1;
    if extra >= too_many:
        raise tried_too_hard_exc;
    return dest;

################################################################################

def result_join (original, sep = '\t'):
    list = [];
    for i in xrange(len(original)):
        if original[i] == None:
            list.append("");
        else:
            list.append(original[i]);
    return sep.join(list);

################################################################################

def prefix_multi_line_string(str, prefix):
    out = "";
    for line in str.split('\n'):
        line = line.strip();
        if line:
            out += "%s%s\n" % (prefix, line);
    # Strip trailing new line
    if out:
        out = out[:-1];
    return out;

################################################################################

def validate_changes_file_arg(file, fatal=1):
    error = None;

    orig_filename = file
    if file.endswith(".katie"):
        file = file[:-6]+".changes";

    if not file.endswith(".changes"):
        error = "invalid file type; not a changes file";
    else:
        if not os.access(file,os.R_OK):
            if os.path.exists(file):
                error = "permission denied";
            else:
                error = "file not found";

    if error:
        if fatal:
            fubar("%s: %s." % (orig_filename, error));
        else:
            warn("Skipping %s - %s" % (orig_filename, error));
            return None;
    else:
        return file;

################################################################################

def real_arch(arch):
    return (arch != "source" and arch != "all");

################################################################################

def join_with_commas_and(list):
	if len(list) == 0: return "nothing";
	if len(list) == 1: return list[0];
	return ", ".join(list[:-1]) + " and " + list[-1];

################################################################################

def get_conf():
	return Cnf;

################################################################################

# Handle -a, -c and -s arguments; returns them as SQL constraints
def parse_args(Options):
    # Process suite
    if Options["Suite"]:
        suite_ids_list = [];
        for suite in Options["Suite"].split():
            suite_id = db_access.get_suite_id(suite);
            if suite_id == -1:
                warn("suite '%s' not recognised." % (suite));
            else:
                suite_ids_list.append(suite_id);
        if suite_ids_list:
            con_suites = "AND su.id IN (%s)" % ", ".join(map(str, suite_ids_list));
        else:
            fubar("No valid suite given.");
    else:
        con_suites = "";

    # Process component
    if Options["Component"]:
        component_ids_list = [];
        for component in Options["Component"].split():
            component_id = db_access.get_component_id(component);
            if component_id == -1:
                warn("component '%s' not recognised." % (component));
            else:
                component_ids_list.append(component_id);
        if component_ids_list:
            con_components = "AND c.id IN (%s)" % ", ".join(map(str, component_ids_list));
        else:
            fubar("No valid component given.");
    else:
        con_components = "";

    # Process architecture
    con_architectures = "";
    if Options["Architecture"]:
        arch_ids_list = [];
        check_source = 0;
        for architecture in Options["Architecture"].split():
            if architecture == "source":
                check_source = 1;
            else:
                architecture_id = db_access.get_architecture_id(architecture);
                if architecture_id == -1:
                    warn("architecture '%s' not recognised." % (architecture));
                else:
                    arch_ids_list.append(architecture_id);
        if arch_ids_list:
            con_architectures = "AND a.id IN (%s)" % ", ".join(map(str, arch_ids_list));
        else:
            if not check_source:
                fubar("No valid architecture given.");
    else:
        check_source = 1;

    return (con_suites, con_architectures, con_components, check_source);

################################################################################

# Inspired(tm) by Bryn Keller's print_exc_plus (See
# http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/52215)

def print_exc():
    tb = sys.exc_info()[2];
    while tb.tb_next:
        tb = tb.tb_next;
    stack = [];
    frame = tb.tb_frame;
    while frame:
        stack.append(frame);
        frame = frame.f_back;
    stack.reverse();
    traceback.print_exc();
    for frame in stack:
        print "\nFrame %s in %s at line %s" % (frame.f_code.co_name,
                                             frame.f_code.co_filename,
                                             frame.f_lineno);
        for key, value in frame.f_locals.items():
            print "\t%20s = " % key,;
            try:
                print value;
            except:
                print "<unable to print>";

################################################################################

apt_pkg.init()

Cnf = apt_pkg.newConfiguration();
apt_pkg.ReadConfigFileISC(Cnf,default_config);

if which_conf_file() != default_config:
	apt_pkg.ReadConfigFileISC(Cnf,which_conf_file())

################################################################################
