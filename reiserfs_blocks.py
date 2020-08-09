#!/usr/bin/env python3
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import array
import collections
import enum
import functools
import heapq
import itertools
import math
import struct
import sys

import parse_ddrescue


@functools.total_ordering
class Range:
    __slots__ = ["start", "size"]

    def __init__(self, start, size):
        self.start = start
        self.size = size

    def __eq__(self, other):
        if type(other) != Range:
            return NotImplemented
        return self.start == other.start and self.size == other.size

    def __lt__(self, other):
        if type(other) != Range:
            return NotImplemented
        if self.start < other.start:
            return True
        if self.start > other.start:
            return False
        return self.size < other.size


# Assumes ranges are added start-to-end and never overlap
class RangeList:
    __slots__ = ["items"]

    def __init__(self):
        self.items = []

    def add(self, start, size):
        if len(self.items) != 0:
            last = self.items[-1]
            if last.start + last.size == start:
                last.size += size
                return
            assert last.start + last.size < start, "false: {} + {} < {}".format(
                last.start, last.size, start
            )
        self.items.append(Range(start, size))


Superblock = collections.namedtuple(
    "Superblock",
    [
        "block_count",
        "free_blocks",
        "root_block",
        "journal_block",
        "journal_device",
        "orig_journal_size",
        "journal_trans_max",
        "journal_magic",
        "journal_max_batch",
        "journal_max_commit_age",
        "journal_max_trans_age",
        "blocksize",
        "oid_max_size",
        "oid_current_size",
        "state",
        "magic_string",
        "hash_function",
        "tree_height",
        "bitmap_number",
        "version",
        "inode_generation",
    ],
)
Superblock.struct = struct.Struct("<11IHHHH12sIHHH2xI")
Superblock.unpack = lambda b: Superblock._make(Superblock.struct.unpack(b))


class Node(
    collections.namedtuple("Node", ["level", "item_count", "free_space", "payload"])
):
    ptr_struct = struct.Struct("<IH2x")
    struct = struct.Struct("<HHHxx16x4072s")

    @staticmethod
    def unpack(b):
        return Node._make(Node.struct.unpack(b))

    @functools.lru_cache(maxsize=128)
    def ptr_find(self, key):
        if self.level == 1:
            return None
        # Comparison is broken for version 1 keys except if one of the types is
        # STAT
        assert key.type == ItemType.STAT
        pos = 0
        for i in range(self.item_count):
            ikey = Key.unpack(self.payload[pos : pos + Key.struct.size])
            if ikey > key:
                break
            pos += Key.struct.size
        else:
            i += 1
        return Node.ptr_struct.unpack_from(
            self.payload, self.item_count * Key.struct.size + i * Node.ptr_struct.size
        )[0]

    def ptr_find_range(self, keyStart, keyEnd):
        """keyStart is inclusive. keyEnd is exclusive."""
        if self.level == 1:
            return None
        pos = 0
        for start in range(self.item_count):
            tmpkey = Key.unpack(self.payload[pos : pos + Key.struct.size])
            if tmpkey > keyStart:
                break
            pos += Key.struct.size
        else:
            start += 1
        end = start - 1
        for end in range(start, self.item_count):
            tmpkey = Key.unpack(self.payload[pos : pos + Key.struct.size])
            if tmpkey >= keyEnd:
                break
            pos += Key.struct.size
        else:
            end += 1

        found = []
        for i in range(start, end + 1):
            found.append(
                Node.ptr_struct.unpack_from(
                    self.payload,
                    self.item_count * Key.struct.size + i * Node.ptr_struct.size,
                )[0]
            )
        return found

    def ptr_blocks(self):
        if self.level == 1:
            return ()
        blocks = array.array(array_4byte_typecode)
        pos = self.item_count * Key.struct.size
        for _ in range(self.item_count + 1):
            blocks.append(Node.ptr_struct.unpack_from(self.payload, pos)[0])
            pos += Node.ptr_struct.size
        return blocks

    def items(self):
        items = []
        for pos in range(0, self.item_count * ItemHdr.struct.size, ItemHdr.struct.size):
            hdr = ItemHdr.unpack(self.payload[pos : pos + ItemHdr.struct.size])
            body = self.payload[hdr.location - 24 : hdr.location - 24 + hdr.length]
            items.append(
                Item(key=hdr.key, count=hdr.count, version=hdr.version, body=body)
            )
        return items

    def item_find(self, key):
        key = key.pack()
        for pos in range(0, self.item_count * ItemHdr.struct.size, ItemHdr.struct.size):
            # Key is first field of ItemHdr
            if key == self.payload[pos : pos + Key.struct.size]:
                hdr = ItemHdr.unpack(self.payload[pos : pos + ItemHdr.struct.size])
                body = self.payload[hdr.location - 24 : hdr.location - 24 + hdr.length]
                return Item(
                    key=hdr.key, count=hdr.count, version=hdr.version, body=body
                )
        return None

    def item_find_range(self, keyStart, keyEnd):
        items = []
        for pos in range(0, self.item_count * ItemHdr.struct.size, ItemHdr.struct.size):
            # Key is first field of ItemHdr
            hdr = ItemHdr.unpack(self.payload[pos : pos + ItemHdr.struct.size])
            if keyStart <= hdr.key and hdr.key < keyEnd:
                body = self.payload[hdr.location - 24 : hdr.location - 24 + hdr.length]
                items.append(
                    Item(key=hdr.key, count=hdr.count, version=hdr.version, body=body)
                )
        return items

    def indirect_item_blocks(self):
        if self.level != 1:
            return ()
        blocks = array.array(array_4byte_typecode)
        for item in self.items():
            if item.key.type != ItemType.INDIRECT:
                continue
            blocks.extend(item.indirect_blocks())
        return blocks


class ItemType(enum.IntEnum):
    STAT = 0
    INDIRECT = 1
    DIRECT = 2
    DIRECTORY = 3
    ANY = 15


ItemType.version1_id2type = {
    0: ItemType.STAT,
    0xFFFFFFFE: ItemType.INDIRECT,
    0xFFFFFFFF: ItemType.DIRECT,
    500: ItemType.DIRECTORY,
    555: ItemType.ANY,
}
ItemType.version1_type2id = {
    ItemType.STAT: 0,
    ItemType.INDIRECT: 0xFFFFFFFE,
    ItemType.DIRECT: 0xFFFFFFFF,
    ItemType.DIRECTORY: 500,
    ItemType.ANY: 555,
}


class Key(
    collections.namedtuple("Key", ["dirid", "objid", "offset", "type", "version"])
):
    struct = struct.Struct("<IIQ")

    @staticmethod
    def unpack(b, version=None):
        parts = list(Key.struct.unpack(b))
        if version is None:
            assumed_type = parts[2] & 0xF
            if assumed_type == 0 or assumed_type == 15:
                version = 1
            else:
                version = 2
        offset_type = parts[2]
        if version == 1:
            parts[2] = offset_type & 0xFFFFFFFF
            parts.append(ItemType.version1_id2type[offset_type >> 32])
            parts.append(1)
        else:
            parts[2] = offset_type & 0x0FFFFFFFFFFFFFFF
            parts.append(ItemType(offset_type >> 60))
            parts.append(2)
        return Key._make(parts)

    def pack(self):
        if self.version == 1:
            parts = (
                self.dirid,
                self.objid,
                self.offset | (ItemType.version1_type2id[self.type] << 32),
            )
        else:
            parts = (self.dirid, self.objid, self.offset | (self.type.value << 60))
        return Key.struct.pack(*parts)


class ItemHdr(
    collections.namedtuple("ItemHdr", ["key", "count", "length", "location", "version"])
):
    struct = struct.Struct("<16sHHHH")

    @staticmethod
    def unpack(b):
        parts = list(ItemHdr.struct.unpack(b))
        parts[0] = Key.unpack(parts[0], version=parts[4] + 1)
        return ItemHdr._make(parts)


DirectoryEntry = collections.namedtuple(
    "DirectoryEntry", ["offset", "dirid", "objid", "name", "state"]
)
# Only the struct for the header; the name is separate
DirectoryEntry.struct = struct.Struct("<IIIHH")


class Stat(
    collections.namedtuple(
        "Stat",
        [
            "mode",
            "filetype",
            "numlinks",
            "uid",
            "gid",
            "size",
            "atime",
            "mtime",
            "ctime",
        ],
    )
):
    ver1_struct = struct.Struct("<HHHH6I")
    ver2_struct = struct.Struct("<H2xIQ7I")

    @staticmethod
    def unpack(b):
        if len(b) == Stat.ver1_struct.size:
            parts = Stat.ver1_struct.unpack(b)
            parts = list(parts[:8])
        else:
            parts = Stat.ver2_struct.unpack(b)
            parts = [
                parts[0],
                parts[1],
                parts[3],
                parts[4],
                parts[2],
                parts[5],
                parts[6],
                parts[7],
            ]
        parts.insert(1, FileType(parts[0] >> 12))
        parts[0] = parts[0] & 0xFFF
        return Stat._make(parts)


class FileType(enum.Enum):
    SOCKET = 12
    LINK = 10
    REGULAR = 8
    BLOCK = 6
    DIRECTORY = 4
    CHARACTER = 2
    FIF0 = 1


class Item(collections.namedtuple("Item", ["key", "count", "version", "body"])):
    def directory_list(self):
        entries = []
        implicitEnd = len(self.body)
        for pos in range(
            0, self.count * DirectoryEntry.struct.size, DirectoryEntry.struct.size
        ):
            entry = list(
                DirectoryEntry.struct.unpack(
                    self.body[pos : pos + DirectoryEntry.struct.size]
                )
            )
            location = entry[3]
            locationEnd = location
            while locationEnd < implicitEnd and self.body[locationEnd] != 0:
                locationEnd += 1
            entry[3] = self.body[location:locationEnd]
            entries.append(DirectoryEntry._make(entry))
            implicitEnd = location
        return entries

    def stat(self):
        return Stat.unpack(self.body)

    def indirect_blocks(self):
        return array.array(array_4byte_typecode, self.body)


if array.array("I").itemsize == 4:
    array_4byte_typecode = "I"
else:
    assert array.array("L").itemsize == 4
    array_4byte_typecode = "L"


class ReiserFs:
    __slots__ = [
        "f",
        "rescue_map",
        "sectors",
        "block_size",
        "sectors_per_block",
        "superblock",
        "incomplete",
        "partition_start",
    ]

    def __init__(self, f, rescue_map):
        self.f = f
        self.rescue_map = rescue_map
        self.sectors = []
        self.incomplete = False
        self.partition_start = 0
        # fake values. Initialized in init()
        self.block_size = 512
        self.sectors_per_block = self.block_size // 512
        self.superblock = None

    def init(self):
        self.sectors.append(65536 // 512)
        if self.rescue_map[65536] != parse_ddrescue.Status.FINISHED:
            return False
        self.superblock = Superblock.unpack(
            self.readBlock(65536 // self.block_size)[:0x50]
        )
        self.block_size = self.superblock.blocksize
        self.sectors_per_block = self.block_size // 512
        return True

    def readBlock(self, block_num):
        self.f.seek(self.partition_start + block_num * self.block_size)
        return self.f.read(self.block_size)

    def isBlockComplete(self, block_num):
        start_sector = block_num * self.block_size
        for sector in range(start_sector, start_sector + self.block_size, 512):
            if self.rescue_map[sector] != parse_ddrescue.Status.FINISHED:
                return False
        return True

    @functools.lru_cache(maxsize=128)
    def readNode(self, block, partial_only=False):
        if not partial_only:
            self.sectors.append(block * self.sectors_per_block)
        if self.rescue_map[block * self.block_size] != parse_ddrescue.Status.FINISHED:
            return (False, None)
        node = Node.unpack(self.readBlock(block))
        if node.level == 1:
            node_size_left = 24 + node.item_count * ItemHdr.struct.size
            node_size_right = self.block_size - node_size_left - node.free_space
        else:
            node_size_left = self.block_size - node.free_space
            node_size_right = 0
        incomplete = False
        for off in set(
            itertools.chain(
                range(1, math.ceil(node_size_left / 512)),
                range(
                    self.sectors_per_block - math.ceil(node_size_right / 512),
                    self.sectors_per_block,
                ),
            )
        ):
            if off == 0:
                continue
            self.sectors.append(block * self.sectors_per_block + off)
            if (
                not incomplete
                and self.rescue_map[block * self.block_size + off * 512]
                != parse_ddrescue.Status.FINISHED
            ):
                incomplete = True
        return (not incomplete, node)

    def find_item(self, key):
        treeBlock = self.superblock.root_block
        while True:
            complete, node = self.readNode(treeBlock)
            if not complete:
                return None
            if node.level == 1:
                return node.item_find(key)
            treeBlock = node.ptr_find(key)

    def iter_items_in_range(self, keyStart, keyEnd, treeBlock=None):
        """keyStart is inclusive. keyEnd is exclusive."""
        if treeBlock is None:
            treeBlock = self.superblock.root_block
        complete, node = self.readNode(treeBlock)
        if not complete:
            return
        if node.level == 1:
            yield from node.item_find_range(keyStart, keyEnd)
            return
        for treeBlock in node.ptr_find_range(keyStart, keyEnd):
            yield from self.iter_items_in_range(keyStart, keyEnd, treeBlock)

    def regular_block_list(self, key):
        assert key.type == ItemType.STAT
        item = self.find_item(key)
        expectedSize = -1
        if item is not None:
            stat = item.stat()
            expectedSize = stat.size
            assert stat.filetype == FileType.REGULAR
        keyStart = Key(key.dirid, key.objid, 1, ItemType.STAT, 1)
        keyEnd = Key(key.dirid, key.objid + 1, 0, ItemType.STAT, 1)
        size = 1
        for item in self.iter_items_in_range(keyStart, keyEnd):
            assert item.key.offset >= size
            if item.key.offset > size:
                self.incomplete = True
                missing = item.key.offset - size
                for _ in range(missing // self.block_size):
                    yield 0
                if missing % self.block_size != 0:
                    yield bytes(missing % self.block_size)
                size += missing
            if item.key.type == ItemType.INDIRECT:
                size += len(item.body) // 4 * self.block_size
                yield from item.indirect_blocks()
            elif item.key.type == ItemType.DIRECT:
                size += len(item.body)
                yield item.body
        if size < expectedSize:
            self.incomplete = True

    def directory_list(self, key):
        assert key.type == ItemType.STAT
        item = self.find_item(key)
        expectedSize = -1
        if item is not None:
            stat = item.stat()
            expectedSize = stat.size
            assert stat.filetype == FileType.DIRECTORY
        # It appears that directory keys mostly use version 1
        keyStart = Key(key.dirid, key.objid, 1, ItemType.DIRECTORY, 1)
        keyEnd = Key(key.dirid, key.objid + 1, 0, ItemType.STAT, 1)
        size = 0
        for item in self.iter_items_in_range(keyStart, keyEnd):
            size += len(item.body)
            yield from item.directory_list()
        if size != expectedSize:
            self.incomplete = True

    def get_name(self, key, parent):
        if key.objid == 2:
            return b""  # root
        for entry in self.directory_list(parent):
            if entry.objid == key.objid:
                return entry.name

    def get_full_name(self, key, parent):
        parts = []
        while True:
            part = self.get_name(key, parent)
            if part is None:
                part = f"{key.dirid}_{key.objid}".encode()
            parts.append(part)
            if key.objid == 2:
                # At the root
                break
            for entry in itertools.islice(self.directory_list(parent), 2):
                if entry.name != b"..":
                    continue
                key = parent
                parent = Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2)
                break
            else:
                break  # Assume this name part was in the dirid_objid format
        parts.reverse()
        return b"/".join(parts)

    def file_indirect_blocks(self, key):
        assert key.type == ItemType.STAT
        keyStart = Key(key.dirid, key.objid, 1, ItemType.INDIRECT, 1)
        keyEnd = Key(key.dirid, key.objid + 1, 0, ItemType.STAT, 1)
        for item in self.iter_items_in_range(keyStart, keyEnd):
            if item.key.type != ItemType.INDIRECT:
                continue
            yield from item.indirect_blocks()

    def path_to_key(self, name):
        parts = name.split(b"/")
        if parts[0]:
            # Unnamed file, identified by dirid_objid
            id_parts = parts[0].split(b"_")
            assert len(id_parts) == 2
            dirKey = Key(int(id_parts[0]), int(id_parts[1]), 0, ItemType.STAT, 2)
        else:
            # Rooted file
            dirKey = Key(1, 2, 0, ItemType.STAT, 2)
        parts = parts[1:]
        for part in parts:
            if part == b"":
                continue
            for entry in self.directory_list(dirKey):
                if part == entry.name:
                    dirKey = Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2)
                    break
            else:
                return None
        return dirKey


def iter_leafs(fs):
    heap = []
    next_pass = [(fs.superblock.root_block, -1)]
    while next_pass:
        heapq.heapify(next_pass)
        tmp = heap
        heap = next_pass
        next_pass = tmp
        next_pass.clear()

        while heap:
            block, level = heapq.heappop(heap)
            complete, node = fs.readNode(block)
            if not complete:
                continue
            if node.level > 1:
                for ptr_block in node.ptr_blocks():
                    if ptr_block < block:
                        next_pass.append((ptr_block, node.level - 1))
                    else:
                        heapq.heappush(heap, (ptr_block, node.level - 1))
            elif node.level == 1:
                yield node


class SetList:
    __slots__ = ["s"]

    def __init__(self):
        self.s = set()

    def append(self, item):
        self.s.add(item)


def find(fs, name):
    if not fs.init():
        print(f"Could not access superblock", file=sys.stderr)
        return

    for leaf in iter_leafs(fs):
        for item in leaf.items():
            if item.key.type != ItemType.DIRECTORY:
                continue
            for entry in item.directory_list():
                if entry.name == name:
                    print(
                        fs.get_full_name(
                            Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2),
                            Key(item.key.dirid, item.key.objid, 0, ItemType.STAT, 2),
                        ).decode(errors="replace")
                    )


def ls(fs, name, recurse=False):
    if not fs.init():
        print(f"Could not access superblock", file=sys.stderr)
        return

    dirKey = fs.path_to_key(name)
    if dirKey is None:
        print(f"Could not find {name.decode()}", file=sys.stderr)
        return

    item = fs.find_item(dirKey)
    if item is None:
        print(f"Could not stat {name.decode()}", file=sys.stderr)
        return
    stat = item.stat()
    if stat.filetype == FileType.REGULAR:
        print(f"{name.decode()} (normal file)", file=sys.stderr)
        return
    if stat.filetype == FileType.LINK:
        print(f"{name.decode()} (symbolic link)", file=sys.stderr)
        return
    if stat.filetype != FileType.DIRECTORY:
        print(f"{name.decode()} (special file)", file=sys.stderr)
        return

    dirname = None
    for entry in itertools.islice(fs.directory_list(dirKey), 2):
        if entry.name != b"..":
            continue
        dirname = fs.get_name(
            dirKey, Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2)
        )
    if dirname is None:
        if recurse:
            dirname = f"{dirKey.dirid}_{dirKey.objid}".encode()
        else:
            dirname = b"(unknown)"
    dirname = dirname.decode(errors="replace")
    dirname += "/"
    ls_(fs, dirKey, dirname, recurse)


def ls_(fs, dirKey, dirname, recurse):
    entries = []
    fs.incomplete = False
    dirList = list(fs.directory_list(dirKey))
    incomplete = fs.incomplete
    for entry in dirList:
        directory = False
        name = entry.name.decode(errors="replace")
        if entry.name == b".":
            if recurse:
                name = dirname
                if incomplete:
                    name += " (incomplete entry list)"
            else:
                name = f"{name: <2}\t{entry.dirid}_{entry.objid}\t{dirname}"
            print(name)
            continue
        if entry.name == b"..":
            if recurse:
                continue
            name = f"{name: <2}\t{entry.dirid}_{entry.objid}"
            print(name)
            continue

        entryKey = Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2)
        item = fs.find_item(entryKey)
        if item is None:
            name += " (incomplete stat info)"
        else:
            stat = item.stat()
            if stat.filetype == FileType.DIRECTORY:
                name += "/"
                directory = True
            elif stat.filetype == FileType.REGULAR:
                fs.incomplete = False
                blocks = list(fs.regular_block_list(Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2)))
                if fs.incomplete:
                    name += " (incomplete block list)"
                else:
                    for block in blocks:
                        if type(block) == bytes:
                            continue
                        if block == 0:  # assume block 0 is for sparse files
                            continue
                        if not fs.isBlockComplete(block):
                            name += " (incomplete data blocks)"
                            break
                blocks = None

        if directory:
            entries.append((name, entryKey))
        else:
            entries.append((name,))
    entries.sort()
    for entry in entries:
        if not recurse:
            print(entry[0])
        else:
            if len(entry) == 1:
                print(dirname + entry[0])
            else:
                ls_(fs, entry[1], dirname + entry[0], recurse)
    if incomplete and not recurse:
        print("(results incomplete)")


def cat(fs, name):
    if not fs.init():
        print(f"Could not access superblock", file=sys.stderr)
        return

    key = fs.path_to_key(name)
    if key is None:
        print(f"Could not find {name.decode()}", file=sys.stderr)
        return

    item = fs.find_item(key)
    if item is None:
        print(f"Could not stat {name.decode()}", file=sys.stderr)
        return
    stat = item.stat()
    if stat.filetype != FileType.REGULAR:
        print(f"{name.decode()} not a regular file: {stat.filetype}", file=sys.stderr)
        return
    expectedSize = stat.size

    fs.incomplete = False
    currentSize = 0
    for block in fs.regular_block_list(key):
        if type(block) == bytes:
            toWrite = block
        elif block == 0:  # assume block 0 is for sparse files
            toWrite = bytes(fs.block_size)
        else:
            toWrite = fs.readBlock(block)
        if currentSize + len(toWrite) > expectedSize:
            toWrite = toWrite[:expectedSize - currentSize]
        sys.stdout.buffer.write(toWrite)
        currentSize += len(toWrite)
    assert expectedSize == currentSize
    if fs.incomplete:
        # TODO: give different exit code? (would also need to check isBlockComplete)
        pass


def findFolder(fs, names, metadata_only=False):
    if not fs.init():
        rangelist = RangeList()
        rangelist.add(65536, 512)
        print_rangelist(fs, rangelist, 1)
        return

    keysRemaining = []
    excludeIds = set()
    for name in names:
        if name.startswith(b"-"):
            exclude = True
            name = name[1:]
        else:
            exclude = False
        key = fs.path_to_key(name)
        if key is None:
            print(f"Could not find {name.decode()}", file=sys.stderr)
            return
        if exclude:
            excludeIds.add(key.objid)
        else:
            keysRemaining.append(key)

    fs.sectors = SetList()
    blocks = set()  # blocks may be repeated due to hard links
    while keysRemaining:
        key = keysRemaining.pop()
        item = fs.find_item(key)
        if item is None:
            continue
        stat = item.stat()
        if stat.filetype == FileType.DIRECTORY:
            for entry in fs.directory_list(key):
                if entry.name == b"." or entry.name == b"..":
                    continue
                if entry.objid in excludeIds:
                    continue
                keysRemaining.append(Key(entry.dirid, entry.objid, 0, ItemType.STAT, 2))
        elif stat.filetype == FileType.REGULAR:
            if metadata_only:
                list(fs.file_indirect_blocks(key))
            else:
                blocks.update(fs.file_indirect_blocks(key))
    rangelist = RangeList()
    blocks = list(blocks)
    blocks.sort()
    for block in blocks:
        rangelist.add(block * fs.sectors_per_block, fs.sectors_per_block)
    ranges = rangelist.items

    rangelist = RangeList()
    fs.sectors = list(fs.sectors.s)
    fs.sectors.sort()
    rangelist = RangeList()
    for sector in fs.sectors:
        rangelist.add(sector, 1)
    ranges += rangelist.items

    rangelist = RangeList()
    ranges.sort()
    for _range in ranges:
        rangelist.add(_range.start, _range.size)

    print_rangelist(fs, rangelist, 512)


def findTree(fs, level_limit=0, partial_only=False):
    if fs.init():
        _findTree(fs, level_limit, partial_only)
    fs.sectors.sort()
    rangelist = RangeList()
    for sector in fs.sectors:
        rangelist.add(sector, 1)
    print_rangelist(fs, rangelist, 512)


def _findTree(fs, level_limit, partial_only):
    incomplete_count = 0
    partial = 0
    found = 1
    heap = []
    next_pass = [(fs.superblock.root_block, -1)]
    while next_pass:
        heapq.heapify(next_pass)
        tmp = heap
        heap = next_pass
        next_pass = tmp
        next_pass.clear()

        while heap:
            block, level = heapq.heappop(heap)
            complete, node = fs.readNode(block, partial_only=partial_only)
            if not complete:
                incomplete_count += 1
                if node is not None:
                    partial += 1
                continue
            if node.level <= level_limit:
                continue
            if node.level > 1:
                for ptr_block in node.ptr_blocks():
                    found += 1
                    if ptr_block < block:
                        next_pass.append((ptr_block, node.level - 1))
                    else:
                        heapq.heappush(heap, (ptr_block, node.level - 1))
            elif node.level == 1:
                for item_block in node.indirect_item_blocks():
                    if item_block == 0:
                        # It's unclear why these exist. Maybe for sparce files?
                        continue
                    for off in range(fs.sectors_per_block):
                        fs.sectors.append(item_block * fs.sectors_per_block + off)
    print("found:", found, file=sys.stderr)
    print("incomplete:", incomplete_count, file=sys.stderr)
    print("partial:", partial, file=sys.stderr)


def findBitmap(fs, metadataOnly=False):
    rangelist = RangeList()
    if not fs.init():
        rangelist.add(65536, 512)
        print_rangelist(fs, rangelist, 1)
        return
    if metadataOnly:
        rangelist.add(65536 // fs.block_size, 1)
        rangelist.add(65536 // fs.block_size + 1, 1)
        for pos in range(
            fs.block_size * 8, fs.superblock.block_count, fs.block_size * 8
        ):
            rangelist.add(pos, 1)
        print_rangelist(fs, rangelist, fs.block_size)
        return

    r = fs.readBlock(65536 // fs.block_size + 1)
    if fs.rescue_map[65536 + fs.block_size] != parse_ddrescue.Status.FINISHED:
        rangelist.add(65536 // fs.block_size + 1, 1)
    markUsed(rangelist, 0, r)
    for pos in range(fs.block_size * 8, fs.superblock.block_count, fs.block_size * 8):
        r = fs.readBlock(pos)
        if fs.rescue_map[pos * fs.block_size] != parse_ddrescue.Status.FINISHED:
            rangelist.add(pos, 1)
        markUsed(rangelist, pos, r)
    print_rangelist(fs, rangelist, fs.block_size)


def markUsed(rangelist, pos, bitmap):
    for i, b in enumerate(bitmap):
        i *= 8
        for bit in range(8):
            if b & (1 << bit):
                rangelist.add(pos + i + bit, 1)


def print_rangelist(fs, rangelist, mult):
    print(0, "*", 1)
    print(0, fs.partition_start, "-")
    end = 0
    for item in rangelist.items:
        if end != item.start:
            print(fs.partition_start + end * mult, (item.start - end) * mult, "-")
        print(fs.partition_start + item.start * mult, item.size * mult, "+")
        end = item.start + item.size
    # FIXME: need ending '-' to avoid ddrescuelog boolean logic strangeness


def main(argv):
    if len(argv) < 4:
        print(f"Usage: {argv[0]} file.bin file.map [--partition-start N] COMMAND [--metadata]", file=sys.stderr)
        print(
            """
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
""",
            file=sys.stderr,
        )
        sys.exit(1)
    filename_bin = argv[1]
    filename_map = argv[2]
    rescue_map = parse_ddrescue.parseDdrescue(filename_map)
    partition_start = 0

    if len(argv) > 4 and argv[3] == "--partition-start":
        partition_start = int(argv[4])
        del argv[3:5]
    rescue_map.offset = partition_start
    with open(filename_bin, "rb") as f:
        fs = ReiserFs(f, rescue_map)
        fs.partition_start = partition_start

        metadata_only = False
        if len(argv) > 4 and argv[4] == "--metadata":
            metadata_only = True
            del argv[4]
        if argv[3] == "bitmap":
            findBitmap(fs, metadataOnly=metadata_only)
        elif argv[3] == "tree":
            level = 0
            if len(argv) >= 5:
                level = int(argv[4])
            if metadata_only:
                level = max(level, 1)
            findTree(fs, level_limit=level, partial_only=False)
        elif argv[3] == "folder":
            if len(argv) < 5:
                print("PATH required", file=sys.stderr)
                sys.exit(1)
            names = []
            for name in argv[4:]:
                names.append(name.encode())
            findFolder(fs, names, metadata_only=metadata_only)
        elif argv[3] == "ls":
            recurse = False
            if len(argv) > 4 and argv[4] == "-R":
                recurse = True
                del argv[4]
            if len(argv) < 5:
                print("PATH required", file=sys.stderr)
                sys.exit(1)
            ls(fs, argv[4].encode(), recurse=recurse)
        elif argv[3] == "cat":
            if len(argv) < 5:
                print("PATH required", file=sys.stderr)
                sys.exit(1)
            cat(fs, argv[4].encode())
        elif argv[3] == "find":
            if len(argv) < 5:
                print("NAME required", file=sys.stderr)
                sys.exit(1)
            find(fs, argv[4].encode())


if __name__ == "__main__":
    main(sys.argv)
