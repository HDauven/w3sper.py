# SPDX-License-Identifier: MPL-2.0
# Portions Copyright (c) DUSK NETWORK. All rights reserved.
"""The small, fixed RKYV vector layouts used by wallet-core (not a generic codec)."""

import struct
from ._validation import octets, uint


def prefixed(data):
    data = octets(data, "archive")
    return struct.pack("<I", uint(len(data), 32, "archive length")) + data


def vector(items, size=None):
    items = tuple(octets(item, "vector item", size) for item in items)
    body = b"".join(items)
    # All wallet-core fixed entries have at least four-byte alignment.
    if len(body) % 4:
        raise ValueError("unaligned fixed vector")
    return body + struct.pack("<iI", -len(body), len(items))


def vector_items(data, size, root=None):
    data = octets(data, "archive")
    root = len(data) - 8 if root is None else root
    if not 0 <= root <= len(data) - 8:
        raise ValueError("truncated vector root")
    offset, count = struct.unpack_from("<iI", data, root)
    start = root + offset
    end = start + count * size
    if not 0 <= start <= end <= root:
        raise ValueError("invalid archived vector range")
    return tuple(data[i:i + size] for i in range(start, end, size))
