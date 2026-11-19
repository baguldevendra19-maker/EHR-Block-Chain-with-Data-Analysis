"""Small SHA-256 blockchain primitives for EHR audit metadata."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

ZERO_HASH = "0" * 64
GENESIS_TIMESTAMP = "1970-01-01T00:00:00.000000+00:00"
VALID_ACTIONS = {"create", "update", "historical_baseline"}
VALID_ACTOR_ROLES = {"admin", "doctor"}


def _timestamp_string(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    raise ValueError("A valid ISO-8601 timestamp is required.")


def _json_value(value):
    if isinstance(value, datetime):
        return _timestamp_string(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def calculate_record_hash(record, medicines=()):
    """Hash an EHR row and its prescriptions without retaining their contents."""
    normalized_record = {
        key: _json_value(value)
        for key, value in sorted(record.items())
    }
    normalized_medicines = [
        {
            key: _json_value(value)
            for key, value in sorted(medicine.items())
            if key != "record_id"
        }
        for medicine in medicines
    ]
    normalized_medicines.sort(
        key=lambda medicine: (
            medicine.get("line_number", 0),
            json.dumps(medicine, sort_keys=True, separators=(",", ":")),
        )
    )
    payload = {"record": normalized_record, "medicines": normalized_medicines}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Block:
    index: int
    timestamp: str
    record_id: int | None
    record_hash: str
    action: str
    actor_role: str
    previous_hash: str
    hash: str = ""

    def __post_init__(self):
        object.__setattr__(self, "timestamp", _timestamp_string(self.timestamp))
        if not self.hash:
            object.__setattr__(self, "hash", self.calculate_hash())

    def calculate_hash(self):
        payload = {
            "index": self.index,
            "timestamp": self.timestamp,
            "record_id": self.record_id,
            "record_hash": self.record_hash,
            "action": self.action,
            "actor_role": self.actor_role,
            "previous_hash": self.previous_hash,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self):
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "record_id": self.record_id,
            "record_hash": self.record_hash,
            "action": self.action,
            "actor_role": self.actor_role,
            "previous_hash": self.previous_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_mapping(cls, block):
        stored_hash = block.get("block_hash", block.get("hash"))
        if not isinstance(stored_hash, str) or not stored_hash:
            raise ValueError("A stored block must include its hash.")
        timestamp = block["timestamp"]
        if isinstance(timestamp, datetime) and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return cls(
            index=int(block["block_index"] if "block_index" in block else block["index"]),
            timestamp=timestamp,
            record_id=(
                int(block["record_id"]) if block.get("record_id") is not None else None
            ),
            record_hash=block["record_hash"],
            action=block["action"],
            actor_role=block["actor_role"],
            previous_hash=block["previous_hash"],
            hash=stored_hash,
        )


class Blockchain:
    def __init__(self, blocks=None):
        self.chain = (
            [self.create_genesis_block()]
            if blocks is None
            else [block if isinstance(block, Block) else Block.from_mapping(block) for block in blocks]
        )

    @staticmethod
    def create_genesis_block():
        return Block(
            index=0,
            timestamp=GENESIS_TIMESTAMP,
            record_id=None,
            record_hash=ZERO_HASH,
            action="genesis",
            actor_role="system",
            previous_hash=ZERO_HASH,
        )

    def add_block(self, record_id, record_hash, action, actor_role, timestamp=None):
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            raise ValueError("record_id must be a positive integer.")
        if (
            not isinstance(record_hash, str)
            or len(record_hash) != 64
            or any(character not in "0123456789abcdef" for character in record_hash.lower())
        ):
            raise ValueError("record_hash must be a SHA-256 hex digest.")
        if action not in VALID_ACTIONS:
            raise ValueError(
                "action must be 'create', 'update', or 'historical_baseline'."
            )
        if actor_role not in VALID_ACTOR_ROLES:
            raise ValueError("actor_role must be an authorized audit role.")
        if not self.chain:
            raise ValueError("The chain must contain its genesis block.")

        block = Block(
            index=len(self.chain),
            timestamp=timestamp or datetime.now(timezone.utc),
            record_id=record_id,
            record_hash=record_hash.lower(),
            action=action,
            actor_role=actor_role,
            previous_hash=self.chain[-1].hash,
        )
        self.chain.append(block)
        return block

    @classmethod
    def from_blocks(cls, blocks):
        return cls(blocks=blocks)

    def is_chain_valid(self):
        if not self.chain:
            return False
        genesis = self.chain[0]
        expected_genesis = self.create_genesis_block()
        if genesis != expected_genesis:
            return False
        for index, block in enumerate(self.chain):
            if block.index != index or block.hash != block.calculate_hash():
                return False
            if index and block.previous_hash != self.chain[index - 1].hash:
                return False
        return True
