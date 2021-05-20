Rescue Tools for Reiserfs
=========================

A collection of tools for aiding recovery of reiserfs partitions with ddrescue.
The tools are for targeting recovery from unreliable media, not file system
corruption; the tools assume that read blocks are not corrupted.

[Ddrescue](https://www.gnu.org/software/ddrescue/) is a helpful tool for
reading data off unstable media. For (mostly) reliable media, ddrescue by
itself is excellent. But for badly damaged media, recovery can take a
significant amount of time and may spend a significant amount of time on unused
or unimportant data.

_This is not an officially supported Google product_. It was simply developed by
a Google engineer to scratch an itch.

I had a crashed 80 GB hard disk from 2006 that I wanted recover more data from
before wiping for recycling. In 2006 I had recovered some data from it by
manually skipping over bad areas with `dd`, but I was time-pressed. Trying
`ddrescue` in 2020 showed remarkable progress and was recovering additional
data, but bit rot severely impacted read speed and success rates. After _weeks_
running ddrescue, reduced read rates meant I needed to be more targeted with my
approach.

parse_ddrescue.py
-----------------

This tool parses a ddrescue map file and generates an image of the current
progress. It allows a view reminiscent of Windows 95's disk defragmenter. For
reliable media, it isn't too interesting, but it can be very informative for
badly damaged disks. You can use the view to track progress as well as target
certain areas with `--input-position` and `--size`.

The output is a PNM, so is nice to convert directly to a png when being
generated.

```sh
$ ./parse_ddrescue.py rescue.map | convert - rescue.png # ImageMagick
$ ./parse_ddrescue.py rescue.map | pnm2png > rescue.png # libpng
```

reiserfs_blocks.py
------------------

This tool is the only reiserfs-specific tool, but is a multitool. Generated
ddrescue maps are intended to be passed to ddrescue via `--domain-mapfile`
(`-m`).

```sh
# ./reiserfs_blocks.py rescue.bin rescue.map bitmap > used.map
# ddrescue /dev/disk/by-id/some-disk-part1 rescue.bin rescue.map -m used.map
# # At times need to re-run reiserfs_blocks so it can read additional info.
# ./reiserfs_blocks.py rescue.bin rescue.map bitmap > used2.map
# # If using --retry-passes (-r), may want to only retry the new data.
# # ddrescuelog can subtract the previous log from the new log.
# ddrescuelog used2.map -y <(ddrescuelog -n used.map) > used2.new.map
# ddrescue /dev/disk/by-id/some-disk-part1 rescue.bin rescue.map \
    -r 30 -m used2.new.map
```

```
Usage: ./reiserfs_blocks.py file.bin file.map [--partition-start N] COMMAND [--metadata]

COMMANDS
  bitmap       Produce ddrescue map of used blocks based on the free space
               bitmaps. This very quickly provides a view of used blocks and is
               a good choice when the vast majority of data is readable. Note
               that data blocks may be thrown away during fsck if the file
               metadata that references them has been lost

               This should be re-run as more bitmaps are recovered from disk to
               provide more complete results

  tree [LEVEL] Produce ddrescue map of used blocks based on the b-tree. This is
               moderate speed and ensures recovery time is only spent on
               accessible data. Specifying LEVEL will limit results to that
               level and higher. Level 0 is file data, level 1 is file
               metadata, and higher levels are used to discover lower levels.
               Specifying level 1 initially is a good idea, and then proceeding
               to 0 after level 1+ has been recovered. If you are needing to
               retry bad blocks, focusing on higher levels (2+) first is a good
               idea as they can "unlock" a substantial amount of lower-level
               data

               This should be re-run as more higher-level blocks are recovered
               from disk to provide more complete results

  folder PATH..
               Produce ddrescue map of used blocks by traversing the directory
               tree, for PATH and its descendants. This allows recovering
               specific data, but can be slow as it needs to be run many times
               as the directory structure is recovered. Multiple paths may be
               specified. If a path is prefixed with dash ('-') it will be
               excluded

               This should be re-run as more directories are recovered from
               disk to provide more complete results. If 'tree 1' has been
               fully recovered, then reruns are unnecessary.

  ls [-R] PATH List the contents of directory found via PATH, denoting
               incomplete files. This must either be an absolute path or a path
               starting with a directory in the form used by lost+found (e.g.,
               1337_1338/some/folder). This is useful for looking through the
               disk without running fsck and checking the recovery status of
               individual files. -R will include transitive contents

  find NAME    Find files with name NAME. This is useful for finding for a
               directory that is not reachable from the root and would exist in
               lost+found after a fsck. For example, home directories could be
               found by searching for '.bashrc'

  cat PATH     Dump file contents to standard out. Intended to allow reading a
               few files without needing to run fsck. Do not fully trust the
               output; consider it a debug or quick-and-dirty tool

OPTIONS
  --partition-start N
               The start of the reiserfs partition in bytes. This is necessary
               if the file is a full disk image; should not be necessary for
               partition images. Defaults to 0

  --metadata   Restrict ddrescue map output to metadata, such as bitmap and
               b-tree blocks
```

ddrescuelog_processor.py
------------------------

This tool assumes that bad blocks near good blocks are more likely to be read
on retry. Given the current mapfile as progress, it "extends" the successful
areas. This generates a mapfile for use with `--domain-mapfile` (`-m`) that
will target bad blocks near existing good blocks. This is mainly helpful when
performing extended retries with `--retry-passes` (`-r`).

```sh
# ddrescue /dev/disk/by-id/some-disk-part1 rescue.bin rescue.map \
    -r 30 -m <(./ddrescuelog_processor.py rescue.map)
# # ddrescuelog be used to filter the results of reiserfs_blocks.py
# ddrescuelog used.map -y <(./ddrescuelog_processor.py rescue.map) > used.near-good.map
# ddrescue /dev/disk/by-id/some-disk-part1 rescue.bin rescue.map \
    -r 30 -m used.near-good.map
```

Reiserfs Documentation
----------------------

The reiserfs disk format seems best documented in [a doc by Florian
Buchholz](https://web.archive.org/web/20111228130817id_/http://homes.cerias.purdue.edu/~florian/reiser/reiserfs.php).
Some things are missing, but can generally be inferred, assumed, or learned
from a real-life partition. Although I didn't make much use of it, [p-nand-q's
reiserfs
documentation](http://p-nand-q.com/download/rfstool/reiserfs_docs.html) may be
helpful.
