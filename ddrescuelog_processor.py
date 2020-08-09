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

import sys

import parse_ddrescue
import reiserfs_blocks


def print_rangelist(rangelist):
    print(0, "*", 1)
    end = 0
    for item in rangelist.items:
        if end != item.start:
            print(end, item.start - end, "-")
        print(item.start, item.size, "+")
        end = item.start + item.size


def main(argv):
    if len(argv) < 2:
        print(f"Usage: {argv[0]} MAPFILE", file=sys.stderr)
        sys.exit(1)
    filenameMap = argv[1]
    rescueMap = parse_ddrescue.parseDdrescue(filenameMap)
    rangelist = reiserfs_blocks.RangeList()
    expandAmount = 512 * 1
    mapSize = rescueMap.size()
    last = 0
    for start, size, val in rescueMap:
        if val != parse_ddrescue.Status.FINISHED:
            continue
        end = min(mapSize, start+size+expandAmount)
        start = max(last, start-expandAmount)
        last = end
        rangelist.add(start, end-start)
    print_rangelist(rangelist)


if __name__ == "__main__":
    main(sys.argv)
