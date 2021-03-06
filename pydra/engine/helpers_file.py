"""Functions ported from Nipype 1, after removing parts that were related to py2."""
import attr
import subprocess as sp
from hashlib import sha256
import os
import os.path as op
import re
import shutil
import posixpath
from builtins import str, bytes, open
import logging
from pathlib import Path

related_filetype_sets = [(".hdr", ".img", ".mat"), (".nii", ".mat"), (".BRIK", ".HEAD")]
"""List of neuroimaging file types that are to be interpreted together."""

logger = logging.getLogger("pydra")


def split_filename(fname):
    """
    Split a filename into parts: path, base filename and extension.

    Parameters
    ----------
    fname : :obj:`str`
        file or path name

    Returns
    -------
    pth : :obj:`str`
        base path from fname
    fname : :obj:`str`
        filename from fname, without extension
    ext : :obj:`str`
        file extension from fname

    Examples
    --------
    >>> pth, fname, ext = split_filename('/home/data/subject.nii.gz')
    >>> pth
    '/home/data'

    >>> fname
    'subject'

    >>> ext
    '.nii.gz'

    """
    special_extensions = [".nii.gz", ".tar.gz", ".niml.dset"]

    pth = op.dirname(fname)
    fname = op.basename(fname)

    ext = None
    for special_ext in special_extensions:
        ext_len = len(special_ext)
        if (len(fname) > ext_len) and (fname[-ext_len:].lower() == special_ext.lower()):
            ext = fname[-ext_len:]
            fname = fname[:-ext_len]
            break
    if not ext:
        fname, ext = op.splitext(fname)

    return pth, fname, ext


def fname_presuffix(fname, prefix="", suffix="", newpath=None, use_ext=True):
    """
    Manipulate path and name of input filename.

    Parameters
    ----------
    fname : :obj:`str`
        A filename (may or may not include path)
    prefix : :obj:`str`
        Characters to prepend to the filename
    suffix : :obj:`str`
        Characters to append to the filename
    newpath : :obj:`str`
        Path to replace the path of the input fname
    use_ext : :obj:`bool`
        If True (default), appends the extension of the original file
        to the output name.

    Return
    ------
    path : :obj:`str`
        Absolute path of the modified filename

    Examples
    --------
    >>> from pydra.engine.helpers_file import fname_presuffix
    >>> fname = 'foo.nii.gz'
    >>> fname_presuffix(fname,'pre','post','/tmp')
    '/tmp/prefoopost.nii.gz'

    """
    pth, fname, ext = split_filename(fname)
    if not use_ext:
        ext = ""

    # No need for isdefined: bool(Undefined) evaluates to False
    if newpath:
        pth = op.abspath(newpath)
    return op.join(pth, prefix + fname + suffix + ext)


def hash_file(afile, chunk_len=8192, crypto=sha256, raise_notfound=True):
    """Compute hash of a file using 'crypto' module."""
    from .specs import LazyField
    from .helpers import hash_function

    # adding option for tasks with splitter over list of files
    if isinstance(afile, list):
        return hash_function([hash_file(el) for el in afile])

    if afile is None or isinstance(afile, LazyField) or isinstance(afile, list):
        return None
    if not os.path.isfile(afile):
        if raise_notfound:
            raise RuntimeError('File "%s" not found.' % afile)
        return None

    crypto_obj = crypto()
    with open(afile, "rb") as fp:
        while True:
            data = fp.read(chunk_len)
            if not data:
                break
            crypto_obj.update(data)
    return crypto_obj.hexdigest()


def _parse_mount_table(exit_code, output):
    """
    Parse the output of ``mount`` to produce (path, fs_type) pairs.

    Separated from _generate_cifs_table to enable testing logic with real
    outputs

    """
    # Not POSIX
    if exit_code != 0:
        return []

    # Linux mount example:  sysfs on /sys type sysfs (rw,nosuid,nodev,noexec)
    #                          <PATH>^^^^      ^^^^^<FSTYPE>
    # OSX mount example:    /dev/disk2 on / (hfs, local, journaled)
    #                               <PATH>^  ^^^<FSTYPE>
    pattern = re.compile(r".*? on (/.*?) (?:type |\()([^\s,\)]+)")

    # Keep line and match for error reporting (match == None on failure)
    # Ignore empty lines
    matches = [(l, pattern.match(l)) for l in output.strip().splitlines() if l]

    # (path, fstype) tuples, sorted by path length (longest first)
    mount_info = sorted(
        (match.groups() for _, match in matches if match is not None),
        key=lambda x: len(x[0]),
        reverse=True,
    )
    cifs_paths = [path for path, fstype in mount_info if fstype.lower() == "cifs"]

    # Report failures as warnings
    for line, match in matches:
        if match is None:
            logger.debug("Cannot parse mount line: '%s'", line)

    return [
        mount
        for mount in mount_info
        if any(mount[0].startswith(path) for path in cifs_paths)
    ]


def _generate_cifs_table():
    """
    Construct a reverse-length-ordered list of mount points that fall under a CIFS mount.

    This precomputation allows efficient checking for whether a given path
    would be on a CIFS filesystem.
    On systems without a ``mount`` command, or with no CIFS mounts, returns an
    empty list.

    """
    exit_code, output = sp.getstatusoutput("mount")
    return _parse_mount_table(exit_code, output)


_cifs_table = _generate_cifs_table()


def on_cifs(fname):
    """
    Check whether a file path is on a CIFS filesystem mounted in a POSIX host.

    POSIX hosts are assumed to have the ``mount`` command.

    On Windows, Docker mounts host directories into containers through CIFS
    shares, which has support for Minshall+French symlinks, or text files that
    the CIFS driver exposes to the OS as symlinks.
    We have found that under concurrent access to the filesystem, this feature
    can result in failures to create or read recently-created symlinks,
    leading to inconsistent behavior and ``FileNotFoundError`` errors.

    This check is written to support disabling symlinks on CIFS shares.

    """
    # Only the first match (most recent parent) counts
    for fspath, fstype in _cifs_table:
        if fname.startswith(fspath):
            return fstype == "cifs"
    return False


def copyfile(
    originalfile,
    newfile,
    copy=False,
    create_new=False,
    use_hardlink=True,
    copy_related_files=True,
):
    """
    Copy or link files.

    If ``use_hardlink`` is True, and the file can be hard-linked, then a
    link is created, instead of copying the file.

    If a hard link is not created and ``copy`` is False, then a symbolic
    link is created.

    .. admonition:: Copy options for existing files

        * symlink

            * to regular file originalfile            (keep if symlinking)
            * to same dest as symlink originalfile    (keep if symlinking)
            * to other file                           (unlink)

        * regular file

            * hard link to originalfile               (keep)
            * copy of file (same hash)                (keep)
            * different file (diff hash)              (unlink)

    .. admonition:: Copy options for new files

        * ``use_hardlink`` & ``can_hardlink`` => hardlink
        * ``~hardlink`` & ``~copy`` & ``can_symlink`` => symlink
        * ``~hardlink`` & ``~symlink`` => copy

    Parameters
    ----------
    originalfile : :obj:`str`
        full path to original file
    newfile : :obj:`str`
        full path to new file
    copy : Bool
        specifies whether to copy or symlink files
        (default=False) but only for POSIX systems
    use_hardlink : Bool
        specifies whether to hard-link files, when able
        (Default=False), taking precedence over copy
    copy_related_files : Bool
        specifies whether to also operate on related files, as defined in
        ``related_filetype_sets``

    Returns
    -------
    None

    """
    newhash = None
    orighash = None
    logger.debug(newfile)

    if create_new:
        while op.exists(newfile):
            base, fname, ext = split_filename(newfile)
            s = re.search("_c[0-9]{4,4}$", fname)
            i = 0
            if s:
                i = int(s.group()[2:]) + 1
                fname = fname[:-6] + "_c%04d" % i
            else:
                fname += "_c%04d" % i
            newfile = base + os.sep + fname + ext

    # Don't try creating symlinks on CIFS
    if copy is False and on_cifs(newfile):
        copy = True

    keep = False
    if op.lexists(newfile):
        if op.islink(newfile):
            if all(
                (
                    os.readlink(newfile) == op.realpath(originalfile),
                    not use_hardlink,
                    not copy,
                )
            ):
                keep = True
        elif posixpath.samefile(newfile, originalfile):
            keep = True
        else:
            newhash = hash_file(newfile)
            logger.debug("File: %s already exists,%s, copy:%d", newfile, newhash, copy)
            orighash = hash_file(originalfile)
            keep = newhash == orighash
        if keep:
            logger.debug(
                "File: %s already exists, not overwriting, copy:%d", newfile, copy
            )
        else:
            os.unlink(newfile)

    if not keep and use_hardlink:
        try:
            logger.debug("Linking File: %s->%s", newfile, originalfile)
            # Use realpath to avoid hardlinking symlinks
            os.link(op.realpath(originalfile), newfile)
        except OSError:
            use_hardlink = False  # Disable hardlink for associated files
        else:
            keep = True

    if not keep and not copy and os.name == "posix":
        try:
            logger.debug("Symlinking File: %s->%s", newfile, originalfile)
            os.symlink(originalfile, newfile)
        except OSError:
            copy = True  # Disable symlink for associated files
        else:
            keep = True

    if not keep:
        try:
            logger.debug("Copying File: %s->%s", newfile, originalfile)
            shutil.copyfile(originalfile, newfile)
        except shutil.Error as e:
            logger.warning(e.message)

    # Associated files
    if copy_related_files:
        related_file_pairs = (
            get_related_files(f, include_this_file=False)
            for f in (originalfile, newfile)
        )
        for alt_ofile, alt_nfile in zip(*related_file_pairs):
            if op.exists(alt_ofile):
                copyfile(
                    alt_ofile,
                    alt_nfile,
                    copy,
                    use_hardlink=use_hardlink,
                    copy_related_files=False,
                )

    return newfile


def get_related_files(filename, include_this_file=True):
    """
    Return a list of related files.

    As defined in :attr:`related_filetype_sets`, for a filename
    (e.g., Nifti-Pair, Analyze (SPM), and AFNI files).

    Parameters
    ----------
    filename : :obj:`str`
        File name to find related filetypes of.
    include_this_file : bool
        If true, output includes the input filename.

    """
    related_files = []
    path, name, this_type = split_filename(filename)
    for type_set in related_filetype_sets:
        if this_type in type_set:
            for related_type in type_set:
                if include_this_file or related_type != this_type:
                    related_files.append(op.join(path, name + related_type))
    if not len(related_files):
        related_files = [filename]
    return related_files


def copyfiles(filelist, dest, copy=False, create_new=False):
    """
    Copy or symlink files in ``filelist`` to ``dest`` directory.

    Parameters
    ----------
    filelist : list
        List of files to copy.
    dest : path/files
        full path to destination. If it is a list of length greater
        than 1, then it assumes that these are the names of the new
        files.
    copy : Bool
        specifies whether to copy or symlink files
        (default=False) but only for posix systems

    Returns
    -------
    None

    """
    outfiles = ensure_list(dest)
    newfiles = []
    for i, f in enumerate(ensure_list(filelist)):
        if isinstance(f, list):
            newfiles.insert(i, copyfiles(f, dest, copy=copy, create_new=create_new))
        else:
            if len(outfiles) > 1:
                destfile = outfiles[i]
            else:
                destfile = fname_presuffix(f, newpath=outfiles[0])
            destfile = copyfile(f, destfile, copy, create_new=create_new)
            newfiles.insert(i, destfile)
    return newfiles


# dj: copied from misc
def is_container(item):
    """
    Check if item is a container (list, tuple, dict, set).

    Parameters
    ----------
    item : :obj:`object`
        Input object to check.

    Returns
    -------
    output : :obj:`bool`
        ``True`` if container ``False`` otherwise.

    """
    if isinstance(item, str):
        return False
    elif hasattr(item, "__iter__"):
        return True

    return False


def ensure_list(filename):
    """Return a list given either a string or a list."""
    if isinstance(filename, (str, bytes)):
        return [filename]
    elif isinstance(filename, list):
        return filename
    elif is_container(filename):
        return [x for x in filename]

    return None


# not sure if this might be useful for Function Task
def copyfile_input(inputs, output_dir):
    """Implement the base class method."""
    from .specs import attr_fields, File

    map_copyfiles = {}
    for fld in attr_fields(inputs):
        copy = fld.metadata.get("copyfile")
        if copy is not None and fld.type is not File:
            raise Exception(
                f"if copyfile set, field has to be a File " f"but {fld.type} provided"
            )
        if copy in [True, False]:
            file = getattr(inputs, fld.name)
            newfile = output_dir.joinpath(Path(getattr(inputs, fld.name)).name)
            copyfile(file, newfile, copy=copy)
            map_copyfiles[fld.name] = str(newfile)
    return map_copyfiles or None


# not sure if this might be useful for Function Task
def template_update(inputs, map_copyfiles=None):
    """
    Update all templates that are present in the input spec.

    Should be run when all inputs used in the templates are already set.

    """
    dict_ = attr.asdict(inputs)
    if map_copyfiles is not None:
        dict_.update(map_copyfiles)

    from .specs import attr_fields

    fields = attr_fields(inputs)
    # TODO: Create a dependency graph first and then traverse it
    for fld in fields:
        if getattr(inputs, fld.name) is not None:
            continue
        if fld.metadata.get("output_file_template"):
            if fld.type is str:
                value = fld.metadata["output_file_template"].format(**dict_)
                dict_[fld.name] = str(value)
            else:
                raise Exception(
                    f"output_file_template metadata for "
                    "{fld.name} should be a string"
                )
    return {k: v for k, v in dict_.items() if getattr(inputs, k) != v}


def is_local_file(f):
    from .specs import File

    return f.type is File and "container_path" not in f.metadata
