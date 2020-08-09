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

import bisect
import enum
import math
import sys


class Status(enum.Enum):
    NON_TRIED = "?"
    NON_TRIMMED = "*"
    NON_SCRAPED = "/"
    BAD = "-"
    FINISHED = "+"


statusToBits = {
    Status.NON_TRIED: 0,
    Status.NON_TRIMMED: 2,
    Status.NON_SCRAPED: 2,
    Status.BAD: 4,
    Status.FINISHED: 1,
}

bitsToColor = {
    0: bytes([0x80, 0x80, 0x80]),
    1: bytes([0xFF, 0xFF, 0xFF]),
    2: bytes([0xFF, 0x80, 0x80]),
    3: bytes([0xFF, 0xA0, 0xA0]),
    4: bytes([0xFF, 0x00, 0x00]),
    5: bytes([0xFF, 0x00, 0x00]),
    6: bytes([0xFF, 0x00, 0x00]),
    7: bytes([0xFF, 0x00, 0x00]),
}


def compute_bitmap(rescue_map, bytes_per_pixel):
    disk_size = rescue_map.size()
    arr = bytearray(disk_size // bytes_per_pixel + 1)
    for start, size, status in rescue_map:
        bits = statusToBits[status]
        start_off = start // bytes_per_pixel
        for off in range((start % bytes_per_pixel + size - 1) // bytes_per_pixel + 1):
            arr[start_off + off] |= bits

    return arr


def hilbertize(arr):
    min_size = math.ceil(math.sqrt(len(arr)))
    order = (min_size - 1).bit_length()
    pow2_size = 2 ** order
    hil = bytearray(pow2_size * pow2_size)
    while len(arr) < len(hil):
        arr.append(0)
    x = -1
    y = 0
    s = 0

    def step(direction):
        nonlocal x
        nonlocal y
        nonlocal s
        direction = (direction + 4) % 4
        if direction == 0:
            x += 1
        elif direction == 1:
            y += 1
        elif direction == 2:
            x -= 1
        else:
            y -= 1
        hil[(y << order) + x] = arr[s]
        s += 1

    def hilbert(direction, rot, order):
        if order == 0:
            return
        direction += rot
        hilbert(direction, -rot, order - 1)
        step(direction)
        direction -= rot
        hilbert(direction, rot, order - 1)
        step(direction)
        hilbert(direction, rot, order - 1)
        direction -= rot
        step(direction)
        hilbert(direction, -rot, order - 1)

    step(0)
    hilbert(0, 1, order)
    return hil


def blockize(arr):
    BLOCK_SIZE = 256
    block_width = int(math.sqrt(BLOCK_SIZE))
    block_height = BLOCK_SIZE // block_width
    BLOCK_SIZE = block_width * block_height

    blocks = math.ceil(len(arr) / BLOCK_SIZE)
    blocks_wide = math.ceil(math.sqrt(blocks))
    blocks_tall = math.ceil(blocks / blocks_wide)
    width = block_width * blocks_wide
    height = block_height * blocks_tall
    blo = bytearray(width * height)

    arr_iter = iter(arr)
    try:
        for block_y in range(blocks_tall):
            _y = block_y * block_height
            for block_x in range(blocks_wide):
                _x = block_x * block_width
                for y in range(block_height):
                    __y = (_y + y) * width
                    for x in range(block_width):
                        blo[__y + _x + x] = next(arr_iter)
    except StopIteration:
        pass
    return (width, height, blo)


def linear(arr):
    ideal = int(math.sqrt(len(arr)))
    log10 = math.log(ideal) / math.log(10)
    # Low and High may be equal
    pow10Low = 10 ** math.floor(log10)
    pow10High = 10 ** math.ceil(log10)
    assert pow10Low <= ideal and ideal <= pow10High
    options = [pow10Low, pow10Low * 2, pow10Low * 5, pow10High]
    width = min(options, key=lambda x: abs(x - ideal))
    height = math.ceil(len(arr) / width)
    return width, height


def dumpImage(f, arr):
    # arr = hilbertize(arr)
    # width, height, arr = blockize(arr)
    width, height = linear(arr)
    f.write("P6 {} {} 255\n".format(width, height).encode("ascii"))
    for x in arr:
        f.write(bitsToColor[x])
    blank = bytes([0, 0, 0])
    for x in range(width * height - len(arr)):
        f.write(blank)
    f.flush()


class RescueMap:
    __slots__ = ["_ranges", "offset"]

    def __init__(self, ranges):
        self._ranges = ranges
        self.offset = 0

    def __iter__(self):
        for start, size, val in self._ranges:
            yield start - self.offset, size, val

    def __getitem__(self, position):
        if type(position) == slice:
            li = []
            for i in range(*position.indices(self.size())):
                li.append(self[i])
            return li
        position = int(position)
        position += self.offset
        i = bisect.bisect_left(self._ranges, (position,))
        if i != len(self._ranges):
            if self._ranges[i][0] == position:
                return self._ranges[i][2]
        if i != 0:
            item = self._ranges[i - 1]
            assert item[0] <= position and item[0] + item[1] > position
            return item[2]
        raise ValueError

    def size(self):
        return self._ranges[-1][0] + self._ranges[-1][1]


def parseDdrescue(filename):
    """Returns a list of (pos, size, status) tuples."""
    rescue_map = []
    with open(filename) as f:
        while True:
            line = f.readline()
            if not line.startswith("#"):
                break
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip().split()
            parts = (int(parts[0], 0), int(parts[1], 0), Status(parts[2]))
            rescue_map.append(parts)
    return RescueMap(rescue_map)


def main(argv):
    BYTES_PER_PIXEL = 128 * 4 * 512
    rescue_map = parseDdrescue(argv[1])
    arr = compute_bitmap(rescue_map, BYTES_PER_PIXEL)
    dumpImage(sys.stdout.buffer, arr)


if __name__ == "__main__":
    main(sys.argv)
