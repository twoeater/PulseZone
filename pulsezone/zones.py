import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import dns.name
import dns.rdataclass
import dns.rdatatype
import dns.tokenizer
import dns.ttl
import dns.zone
import dns.zonefile


@dataclass
class Scan:
    parsed: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    seen: Set[str] = field(default_factory=set)
    errors: Dict[str, str] = field(default_factory=dict)
    complete: bool = True


def infer_origin(text: str, filename: str) -> dns.name.Name:
    """Only infer a parsing origin; never rewrite the configured HTTP domain."""
    tok = dns.tokenizer.Tokenizer(text)
    soa = None
    saw_record = False
    line = []
    while True:
        token = tok.get()
        if token.is_eol_or_eof():
            if line:
                if not saw_record and line[0].upper() == "$ORIGIN" and len(line) == 2:
                    return dns.name.from_text(line[1])
                if soa is None and any(v.upper() == "SOA" for v in line[1:4]) and line[0].endswith("."):
                    soa = dns.name.from_text(line[0])
                if not line[0].startswith("$"):
                    saw_record = True
            line = []
            if token.is_eof():
                break
        else:
            line.append(token.value)
    fallback = filename[3:] if filename.startswith("db.") else filename
    return soa or dns.name.from_text(fallback)


class ZoneParser:
    """Use dnspython's RR parser, with a root-confined include reader.

    The Reader interface is pinned to dnspython 2.7.0. Include streams share
    one transaction but inherit and restore owner/origin/TTL state like BIND.
    No cwd changes, temporary files, or module-wide monkeypatching are needed.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("$INCLUDE는 ZONE_PATH 내부 파일만 허용합니다")
        if not resolved.is_file():
            raise ValueError("일반 파일이 아닙니다")
        return resolved

    def parse(self, path: Path) -> List[Tuple[str, str]]:
        path = self._path(path)
        text = path.read_text(encoding="utf-8-sig")
        origin = infer_origin(text, path.name)
        zone = dns.zone.Zone(origin)
        with zone.writer() as txn:
            self._read(path, text, txn, (), None)
        result = set()
        for name, _, data in zone.iterate_rdatas(dns.rdatatype.A):
            result.add((name.to_text().lower(), data.address))
        return sorted(result)

    def _read(self, path, text, txn, stack, parent, include_origin=None):
        if path in stack or len(stack) >= 32:
            raise ValueError("$INCLUDE 순환 참조 또는 깊이 제한 초과: " + str(path))
        tokenizer = dns.tokenizer.Tokenizer(text, filename=str(path))
        reader = dns.zonefile.Reader(tokenizer, dns.rdataclass.IN, txn, allow_include=False)
        reader.default_ttl = 300
        reader.default_ttl_known = True
        if parent is not None:
            for key in ("current_origin", "last_name", "last_ttl", "last_ttl_known",
                        "default_ttl", "default_ttl_known"):
                setattr(reader, key, getattr(parent, key))
            if include_origin is not None:
                reader.current_origin = include_origin
        while True:
            token = tokenizer.get(want_leading=True, want_comment=True)
            if token.is_eof():
                return
            if token.is_eol():
                continue
            if token.is_comment():
                tokenizer.get_eol()
                continue
            if not token.value.startswith("$"):
                tokenizer.unget(token)
                reader._rr_line()
                continue
            directive = token.value.upper()
            if directive == "$TTL":
                reader.default_ttl = dns.ttl.from_text(tokenizer.get_string())
                reader.default_ttl_known = True
                tokenizer.get_eol()
            elif directive == "$ORIGIN":
                reader.current_origin = tokenizer.get_name(origin=reader.current_origin)
                tokenizer.get_eol()
            elif directive == "$GENERATE":
                reader._generate_line()
            elif directive == "$INCLUDE":
                name = tokenizer.get_string()
                next_token = tokenizer.get()
                child_origin = reader.current_origin
                if not next_token.is_eol_or_eof():
                    child_origin = dns.name.from_text(next_token.value, reader.current_origin)
                    tokenizer.get_eol()
                child = self._path(path.parent / name)
                self._read(child, child.read_text(encoding="utf-8-sig"), txn,
                           stack + (path,), reader, child_origin)
            else:
                raise ValueError("지원하지 않는 존 지시어: " + directive)


def scan_zones(root: Path, exclude: Optional[Set[Path]] = None) -> Scan:
    result = Scan()
    parser = ZoneParser(root)
    exclude = {p.resolve() for p in (exclude or set())}

    def walk_error(error):
        result.complete = False
        result.errors["scan"] = str(error)

    if not root.is_dir():
        result.complete = False
        result.errors["scan"] = "ZONE_PATH 디렉터리를 읽을 수 없습니다: " + str(root)
        return result
    for directory, subdirs, filenames in os.walk(str(root), onerror=walk_error, followlinks=False):
        subdirs[:] = sorted(d for d in subdirs if not (Path(directory) / d).is_symlink())
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.is_symlink() or not path.is_file() or path.resolve() in exclude:
                continue
            relative = path.relative_to(root).as_posix()
            result.seen.add(relative)
            try:
                result.parsed[relative] = parser.parse(path)
            except Exception as error:
                result.errors[relative] = str(error)
    return result
