#!/usr/bin/env python

"""
Script to automate some parts of checking NEW packages

@contact: Debian FTP Master <ftpmaster@debian.org>
@copyright: 2000, 2001, 2002, 2003, 2006  James Troup <james@nocrew.org>
@copyright: 2009  Joerg Jaspert <joerg@debian.org>
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

# <Omnic> elmo wrote docs?!!?!?!?!?!?!
# <aj> as if he wasn't scary enough before!!
# * aj imagines a little red furry toy sitting hunched over a computer
#   tapping furiously and giggling to himself
# <aj> eventually he stops, and his heads slowly spins around and you
#      see this really evil grin and then he sees you, and picks up a
#      knife from beside the keyboard and throws it at you, and as you
#      breathe your last breath, he starts giggling again
# <aj> but i should be telling this to my psychiatrist, not you guys,
#      right? :)

################################################################################

import errno
import os
import re
import sys
import md5
import apt_pkg
import apt_inst
import shutil
import commands

from daklib import utils
from daklib.dbconn import DBConn, get_binary_from_name_suite
from daklib.regexes import html_escaping, re_html_escaping, re_version, re_spacestrip, \
                           re_contrib, re_nonfree, re_localhost, re_newlinespace, \
                           re_package, re_doc_directory

################################################################################

Cnf = None
Cnf = utils.get_conf()

printed_copyrights = {}
package_relations = {}           #: Store relations of packages for later output

# default is to not output html.
use_html = 0

################################################################################

def usage (exit_code=0):
    print """Usage: dak examine-package [PACKAGE]...
Check NEW package(s).

  -h, --help                 show this help and exit
  -H, --html-output          output html page with inspection result
  -f, --file-name            filename for the html page

PACKAGE can be a .changes, .dsc, .deb or .udeb filename."""

    sys.exit(exit_code)

################################################################################
# probably xml.sax.saxutils would work as well

def escape_if_needed(s):
    if use_html:
        return re_html_escaping.sub(lambda x: html_escaping.get(x.group(0)), s)
    else:
        return s

def headline(s, level=2, bodyelement=None):
    if use_html:
        if bodyelement:
            return """<thead>
                <tr><th colspan="2" class="title" onclick="toggle('%(bodyelement)s', 'table-row-group', 'table-row-group')">%(title)s <span class="toggle-msg">(click to toggle)</span></th></tr>
              </thead>\n"""%{"bodyelement":bodyelement,"title":utils.html_escape(s)}
        else:
            return "<h%d>%s</h%d>\n" % (level, utils.html_escape(s), level)
    else:
        return "---- %s ----\n" % (s)

# Colour definitions, 'end' isn't really for use

ansi_colours = {
  'main': "\033[36m",
  'contrib': "\033[33m",
  'nonfree': "\033[31m",
  'arch': "\033[32m",
  'end': "\033[0m",
  'bold': "\033[1m",
  'maintainer': "\033[32m"}

html_colours = {
  'main': ('<span style="color: aqua">',"</span>"),
  'contrib': ('<span style="color: yellow">',"</span>"),
  'nonfree': ('<span style="color: red">',"</span>"),
  'arch': ('<span style="color: green">',"</span>"),
  'bold': ('<span style="font-weight: bold">',"</span>"),
  'maintainer': ('<span style="color: green">',"</span>")}

def colour_output(s, colour):
    if use_html:
        return ("%s%s%s" % (html_colours[colour][0], utils.html_escape(s), html_colours[colour][1]))
    else:
        return ("%s%s%s" % (ansi_colours[colour], s, ansi_colours['end']))

def escaped_text(s, strip=False):
    if use_html:
        if strip:
            s = s.strip()
        return "<pre>%s</pre>" % (s)
    else:
        return s

def formatted_text(s, strip=False):
    if use_html:
        if strip:
            s = s.strip()
        return "<pre>%s</pre>" % (utils.html_escape(s))
    else:
        return s

def output_row(s):
    if use_html:
        return """<tr><td>"""+s+"""</td></tr>"""
    else:
        return s

def format_field(k,v):
    if use_html:
        return """<tr><td class="key">%s:</td><td class="val">%s</td></tr>"""%(k,v)
    else:
        return "%s: %s"%(k,v)

def foldable_output(title, elementnameprefix, content, norow=False):
    d = {'elementnameprefix':elementnameprefix}
    result = ''
    if use_html:
        result += """<div id="%(elementnameprefix)s-wrap"><a name="%(elementnameprefix)s" />
                   <table class="infobox rfc822">\n"""%d
    result += headline(title, bodyelement="%(elementnameprefix)s-body"%d)
    if use_html:
        result += """    <tbody id="%(elementnameprefix)s-body" class="infobody">\n"""%d
    if norow:
        result += content + "\n"
    else:
        result += output_row(content) + "\n"
    if use_html:
        result += """</tbody></table></div>"""
    return result

################################################################################

def get_depends_parts(depend) :
    v_match = re_version.match(depend)
    if v_match:
        d_parts = { 'name' : v_match.group(1), 'version' : v_match.group(2) }
    else :
        d_parts = { 'name' : depend , 'version' : '' }
    return d_parts

def get_or_list(depend) :
    or_list = depend.split("|")
    return or_list

def get_comma_list(depend) :
    dep_list = depend.split(",")
    return dep_list

def split_depends (d_str) :
    # creates a list of lists of dictionaries of depends (package,version relation)

    d_str = re_spacestrip.sub('',d_str)
    depends_tree = []
    # first split depends string up amongs comma delimiter
    dep_list = get_comma_list(d_str)
    d = 0
    while d < len(dep_list):
        # put depends into their own list
        depends_tree.append([dep_list[d]])
        d += 1
    d = 0
    while d < len(depends_tree):
        k = 0
        # split up Or'd depends into a multi-item list
        depends_tree[d] = get_or_list(depends_tree[d][0])
        while k < len(depends_tree[d]):
            # split depends into {package, version relation}
            depends_tree[d][k] = get_depends_parts(depends_tree[d][k])
            k += 1
        d += 1
    return depends_tree

def read_control (filename):
    recommends = []
    depends = []
    section = ''
    maintainer = ''
    arch = ''

    deb_file = utils.open_file(filename)
    try:
        extracts = apt_inst.debExtractControl(deb_file)
        control = apt_pkg.ParseSection(extracts)
    except:
        print formatted_text("can't parse control info")
        deb_file.close()
        raise

    deb_file.close()

    control_keys = control.keys()

    if control.has_key("Depends"):
        depends_str = control.Find("Depends")
        # create list of dependancy lists
        depends = split_depends(depends_str)

    if control.has_key("Recommends"):
        recommends_str = control.Find("Recommends")
        recommends = split_depends(recommends_str)

    if control.has_key("Section"):
        section_str = control.Find("Section")

        c_match = re_contrib.search(section_str)
        nf_match = re_nonfree.search(section_str)
        if c_match :
            # contrib colour
            section = colour_output(section_str, 'contrib')
        elif nf_match :
            # non-free colour
            section = colour_output(section_str, 'nonfree')
        else :
            # main
            section = colour_output(section_str, 'main')
    if control.has_key("Architecture"):
        arch_str = control.Find("Architecture")
        arch = colour_output(arch_str, 'arch')

    if control.has_key("Maintainer"):
        maintainer = control.Find("Maintainer")
        localhost = re_localhost.search(maintainer)
        if localhost:
            #highlight bad email
            maintainer = colour_output(maintainer, 'maintainer')
        else:
            maintainer = escape_if_needed(maintainer)

    return (control, control_keys, section, depends, recommends, arch, maintainer)

def read_changes_or_dsc (suite, filename):
    dsc = {}

    dsc_file = utils.open_file(filename)
    try:
        dsc = utils.parse_changes(filename, dsc_file=1)
    except:
        return formatted_text("can't parse .dsc control info")
    dsc_file.close()

    filecontents = strip_pgp_signature(filename)
    keysinorder = []
    for l in filecontents.split('\n'):
        m = re.match(r'([-a-zA-Z0-9]*):', l)
        if m:
            keysinorder.append(m.group(1))

    for k in dsc.keys():
        if k in ("build-depends","build-depends-indep"):
            dsc[k] = create_depends_string(suite, split_depends(dsc[k]))
        elif k == "architecture":
            if (dsc["architecture"] != "any"):
                dsc['architecture'] = colour_output(dsc["architecture"], 'arch')
        elif k in ("files","changes","description"):
            if use_html:
                dsc[k] = formatted_text(dsc[k], strip=True)
            else:
                dsc[k] = ('\n'+'\n'.join(map(lambda x: ' '+x, dsc[k].split('\n')))).rstrip()
        else:
            dsc[k] = escape_if_needed(dsc[k])

    keysinorder = filter(lambda x: not x.lower().startswith('checksums-'), keysinorder)

    filecontents = '\n'.join(map(lambda x: format_field(x,dsc[x.lower()]), keysinorder))+'\n'
    return filecontents

def create_depends_string (suite, depends_tree):
    result = ""
    if suite == 'experimental':
        suite_where = "in ('experimental','unstable')"
    else:
        suite_where = "= '%s'" % suite

    comma_count = 1
    session = DBConn().session()
    for l in depends_tree:
        if (comma_count >= 2):
            result += ", "
        or_count = 1
        for d in l:
            if (or_count >= 2 ):
                result += " | "
            # doesn't do version lookup yet.

            res = get_binary_from_name_suite(d['name'], suite_where)
            if res.rowcount > 0:
                i = res.fetchone()

                adepends = d['name']
                if d['version'] != '' :
                    adepends += " (%s)" % (d['version'])

                if i[2] == "contrib":
                    result += colour_output(adepends, "contrib")
                elif i[2] == "non-free":
                    result += colour_output(adepends, "nonfree")
                else :
                    result += colour_output(adepends, "main")
            else:
                adepends = d['name']
                if d['version'] != '' :
                    adepends += " (%s)" % (d['version'])
                result += colour_output(adepends, "bold")
            or_count += 1
        comma_count += 1
    return result

def output_package_relations ():
    """
    Output the package relations, if there is more than one package checked in this run.
    """

    if len(package_relations) < 2:
        # Only list something if we have more than one binary to compare
        package_relations.clear()
        return

    to_print = ""
    for package in package_relations:
        for relation in package_relations[package]:
            to_print += "%-15s: (%s) %s\n" % (package, relation, package_relations[package][relation])

    package_relations.clear()
    print foldable_output("Package relations", "relations", to_print)

def output_deb_info(suite, filename, packagename):
    (control, control_keys, section, depends, recommends, arch, maintainer) = read_control(filename)

    if control == '':
        return formatted_text("no control info")
    to_print = ""
    if not package_relations.has_key(packagename):
        package_relations[packagename] = {}
    for key in control_keys :
        if key == 'Depends':
            field_value = create_depends_string(suite, depends)
            package_relations[packagename][key] = field_value
        elif key == 'Recommends':
            field_value = create_depends_string(suite, recommends)
            package_relations[packagename][key] = field_value
        elif key == 'Section':
            field_value = section
        elif key == 'Architecture':
            field_value = arch
        elif key == 'Maintainer':
            field_value = maintainer
        elif key == 'Description':
            if use_html:
                field_value = formatted_text(control.Find(key), strip=True)
            else:
                desc = control.Find(key)
                desc = re_newlinespace.sub('\n ', desc)
                field_value = escape_if_needed(desc)
        else:
            field_value = escape_if_needed(control.Find(key))
        to_print += " "+format_field(key,field_value)+'\n'
    return to_print

def do_command (command, filename, escaped=0):
    o = os.popen("%s %s" % (command, filename))
    if escaped:
        return escaped_text(o.read())
    else:
        return formatted_text(o.read())

def do_lintian (filename):
    if use_html:
        return do_command("lintian --show-overrides --color html", filename, 1)
    else:
        return do_command("lintian --show-overrides --color always", filename, 1)

def get_copyright (deb_filename):
    package = re_package.sub(r'\1', deb_filename)
    o = os.popen("dpkg-deb -c %s | egrep 'usr(/share)?/doc/[^/]*/copyright' | awk '{print $6}' | head -n 1" % (deb_filename))
    cright = o.read()[:-1]

    if cright == "":
        return formatted_text("WARNING: No copyright found, please check package manually.")

    doc_directory = re_doc_directory.sub(r'\1', cright)
    if package != doc_directory:
        return formatted_text("WARNING: wrong doc directory (expected %s, got %s)." % (package, doc_directory))

    o = os.popen("dpkg-deb --fsys-tarfile %s | tar xvOf - %s 2>/dev/null" % (deb_filename, cright))
    cright = o.read()
    copyrightmd5 = md5.md5(cright).hexdigest()

    res = ""
    if printed_copyrights.has_key(copyrightmd5) and printed_copyrights[copyrightmd5] != "%s (%s)" % (package, deb_filename):
        res += formatted_text( "NOTE: Copyright is the same as %s.\n\n" % \
                               (printed_copyrights[copyrightmd5]))
    else:
        printed_copyrights[copyrightmd5] = "%s (%s)" % (package, deb_filename)
    return res+formatted_text(cright)

def get_readme_source (dsc_filename):
    tempdir = utils.temp_dirname()
    os.rmdir(tempdir)

    cmd = "dpkg-source --no-check --no-copy -x %s %s" % (dsc_filename, tempdir)
    (result, output) = commands.getstatusoutput(cmd)
    if (result != 0):
        res = "How is education supposed to make me feel smarter? Besides, every time I learn something new, it pushes some\n old stuff out of my brain. Remember when I took that home winemaking course, and I forgot how to drive?\n"
        res += "Error, couldn't extract source, WTF?\n"
        res += "'dpkg-source -x' failed. return code: %s.\n\n" % (result)
        res += output
        return res

    path = os.path.join(tempdir, 'debian/README.source')
    res = ""
    if os.path.exists(path):
        res += do_command("cat", path)
    else:
        res += "No README.source in this package\n\n"

    try:
        shutil.rmtree(tempdir)
    except OSError, e:
        if errno.errorcode[e.errno] != 'EACCES':
            res += "%s: couldn't remove tmp dir %s for source tree." % (dsc_filename, tempdir)

    return res

def check_dsc (suite, dsc_filename):
    (dsc) = read_changes_or_dsc(suite, dsc_filename)
    print foldable_output(dsc_filename, "dsc", dsc, norow=True)
    print foldable_output("lintian check for %s" % dsc_filename, "source-lintian", do_lintian(dsc_filename))
    print foldable_output("README.source for %s" % dsc_filename, "source-readmesource", get_readme_source(dsc_filename))

def check_deb (suite, deb_filename):
    filename = os.path.basename(deb_filename)
    packagename = filename.split('_')[0]

    if filename.endswith(".udeb"):
        is_a_udeb = 1
    else:
        is_a_udeb = 0


    print foldable_output("control file for %s" % (filename), "binary-%s-control"%packagename,
                    output_deb_info(suite, deb_filename, packagename), norow=True)

    if is_a_udeb:
        print foldable_output("skipping lintian check for udeb", "binary-%s-lintian"%packagename,
                        "")
    else:
        print foldable_output("lintian check for %s" % (filename), "binary-%s-lintian"%packagename,
                        do_lintian(deb_filename))

    print foldable_output("contents of %s" % (filename), "binary-%s-contents"%packagename,
                    do_command("dpkg -c", deb_filename))

    if is_a_udeb:
        print foldable_output("skipping copyright for udeb", "binary-%s-copyright"%packagename,
                        "")
    else:
        print foldable_output("copyright of %s" % (filename), "binary-%s-copyright"%packagename,
                        get_copyright(deb_filename))

    print foldable_output("file listing of %s" % (filename),  "binary-%s-file-listing"%packagename,
                    do_command("ls -l", deb_filename))

# Read a file, strip the signature and return the modified contents as
# a string.
def strip_pgp_signature (filename):
    inputfile = utils.open_file (filename)
    contents = ""
    inside_signature = 0
    skip_next = 0
    for line in inputfile.readlines():
        if line[:-1] == "":
            continue
        if inside_signature:
            continue
        if skip_next:
            skip_next = 0
            continue
        if line.startswith("-----BEGIN PGP SIGNED MESSAGE"):
            skip_next = 1
            continue
        if line.startswith("-----BEGIN PGP SIGNATURE"):
            inside_signature = 1
            continue
        if line.startswith("-----END PGP SIGNATURE"):
            inside_signature = 0
            continue
        contents += line
    inputfile.close()
    return contents

def display_changes(suite, changes_filename):
    changes = read_changes_or_dsc(suite, changes_filename)
    print foldable_output(changes_filename, "changes", changes, norow=True)

def check_changes (changes_filename):
    try:
        changes = utils.parse_changes (changes_filename)
    except ChangesUnicodeError:
        utils.warn("Encoding problem with changes file %s" % (changes_filename))
    display_changes(changes['distribution'], changes_filename)

    files = utils.build_file_list(changes)
    for f in files.keys():
        if f.endswith(".deb") or f.endswith(".udeb"):
            check_deb(changes['distribution'], f)
        if f.endswith(".dsc"):
            check_dsc(changes['distribution'], f)
        # else: => byhand

def main ():
    global Cnf, db_files, waste, excluded

#    Cnf = utils.get_conf()

    Arguments = [('h',"help","Examine-Package::Options::Help"),
                 ('H',"html-output","Examine-Package::Options::Html-Output"),
                ]
    for i in [ "Help", "Html-Output", "partial-html" ]:
        if not Cnf.has_key("Examine-Package::Options::%s" % (i)):
            Cnf["Examine-Package::Options::%s" % (i)] = ""

    args = apt_pkg.ParseCommandLine(Cnf,Arguments,sys.argv)
    Options = Cnf.SubTree("Examine-Package::Options")

    if Options["Help"]:
        usage()

    stdout_fd = sys.stdout

    for f in args:
        try:
            if not Options["Html-Output"]:
                # Pipe output for each argument through less
                less_fd = os.popen("less -R -", 'w', 0)
                # -R added to display raw control chars for colour
                sys.stdout = less_fd
            try:
                if f.endswith(".changes"):
                    check_changes(f)
                elif f.endswith(".deb") or f.endswith(".udeb"):
                    # default to unstable when we don't have a .changes file
                    # perhaps this should be a command line option?
                    check_deb('unstable', f)
                elif f.endswith(".dsc"):
                    check_dsc('unstable', f)
                else:
                    utils.fubar("Unrecognised file type: '%s'." % (f))
            finally:
                output_package_relations()
                if not Options["Html-Output"]:
                    # Reset stdout here so future less invocations aren't FUBAR
                    less_fd.close()
                    sys.stdout = stdout_fd
        except IOError, e:
            if errno.errorcode[e.errno] == 'EPIPE':
                utils.warn("[examine-package] Caught EPIPE; skipping.")
                pass
            else:
                raise
        except KeyboardInterrupt:
            utils.warn("[examine-package] Caught C-c; skipping.")
            pass

#######################################################################################

if __name__ == '__main__':
    main()
