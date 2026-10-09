# -*- coding: utf-8 -*-
"""Backward-compatibility layer forwarding to akphone_dispatcher.core.payload."""

import os
import sys

SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(SRC_DIR) and SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from akphone_dispatcher.core.payload import (
    clean_completed_payloads,
    digest,
    recover_pending,
    transfer_payload,
    TRANSFER_LOCK,
)

__all__ = [
    "clean_completed_payloads",
    "digest",
    "recover_pending",
    "transfer_payload",
    "TRANSFER_LOCK",
]
