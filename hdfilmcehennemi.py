#!/usr/bin/env python3
"""
USAGE:
  python3 hdfilmcehennemi.py "https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/"
  python3 hdfilmcehennemi.py "https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/" -o film.mp4
  python3 hdfilmcehennemi.py "URL" --limit 20  # only first 20 segments for testing
"""
import base64
import binascii
import collections
import contextlib
import html
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}
TIMEOUT = 15
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_SEGMENT_BYTES = 30 * 1024 * 1024

RATE_LIMIT_RPS = 8
RATE_LIMIT_WINDOW = 1.0
_rate_lock = threading.Lock()
_request_times: collections.deque = collections.deque()


def _rate_limit_wait() -> None:
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _request_times and now - _request_times[0] > RATE_LIMIT_WINDOW:
                _request_times.popleft()
            if len(_request_times) < RATE_LIMIT_RPS:
                _request_times.append(now)
                return
            sleep_time = RATE_LIMIT_WINDOW - (now - _request_times[0])
            if sleep_time <= 0:
                sleep_time = 0.05
        time.sleep(sleep_time)


def _get_retry_after_seconds(headers: object, default: float) -> float:
    try:
        val = headers.get("Retry-After")  # type: ignore[union-attr]
    except AttributeError:
        return default
    if val is None:
        return default
    val = val.strip()
    try:
        return float(val)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(val)  # type: ignore[arg-type]
        if dt is not None:
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            delta = (dt - now).total_seconds()
            if delta > 0:
                return min(delta, 60.0)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default
    return default


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("URL cannot be empty")
    if len(url) > 2048:
        raise ValueError("URL too long")
    if "\x00" in url or "\n" in url or "\r" in url:
        raise ValueError("URL contains invalid characters")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http/https: {url[:100]}")
    if not parsed.hostname:
        raise ValueError(f"URL hostname missing: {url[:100]}")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain user credentials")
    host = parsed.hostname.lower()
    # allowlist for known hosts
    allowed_re = re.compile(
        r"^(.*\.)?hdfilmcehennemi\.(nl|mobi|com|ws)$|^srv\d+\.cdnimages\d+\.shop$|^(.*\.)?cdnimages\d*\.shop$|^hls\d+\.playmix\.uno$|^(.*\.)?playmix\.uno$"
    )
    if not allowed_re.match(host):
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise ValueError(f"URL host is private: {host}")
            raise ValueError(f"URL host not allowed: {host}")
        except ValueError as ve:
            if "private" in str(ve).lower() or "not allowed" in str(ve).lower():
                raise
            # try DNS resolution for private IP
            try:
                for res in socket.getaddrinfo(host, None):
                    ip_str = res[4][0]
                    try:
                        ip2 = ipaddress.ip_address(ip_str)
                        if ip2.is_private or ip2.is_loopback or ip2.is_link_local or ip2.is_multicast or ip2.is_reserved:
                            raise ValueError(f"URL resolves to private IP: {host} -> {ip_str}")
                    except ValueError:
                        continue
            except (socket.gaierror, ValueError) as e:
                if isinstance(e, ValueError) and "private" in str(e).lower():
                    raise
            raise ValueError(f"URL host not allowed: {host}") from None
    else:
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError(f"URL host is private: {host}")
        except ValueError:
            pass
    if "@" in url.split("://", 1)[-1].split("?", 1)[0].split("#", 1)[0] and parsed.username is None and "@" in url:
        raise ValueError("URL contains @")
    return url


def _safe_truncate(text: str, limit: int = 500_000) -> str:
    if len(text) > limit:
        return text[:limit]
    return text


def _b64_to_latin1(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    data = base64.b64decode(s + pad, validate=False)
    return data.decode("latin1")


def _rot_decode(s: str, tlz: int) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if 65 <= code <= 90:
            base = 65
            out.append(chr((code - base + tlz) % 26 + base))
        elif 97 <= code <= 122:
            base = 97
            out.append(chr((code - base + tlz) % 26 + base))
        else:
            out.append(ch)
    return "".join(out)


def _js_skhyr_decode(arr, o3q: str, yetsn: str):
    sixz5 = "".join(arr)
    if len(arr) > 999999:
        try:
            sixz5 = _b64_to_latin1("".join(reversed(sixz5)))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            sixz5 = "".join(sixz5)
    x0q = 0
    nie = 0
    for idx, ch in enumerate(o3q):
        zhe = ord(ch)
        x0q = (x0q * 31 + zhe) % 251
        nie = (nie ^ (zhe + idx)) & 255
    tpx = (x0q + nie) % 256
    huox = (x0q % 13) + 3
    nbzvj = ((x0q * 256 + nie) % 65521) + 1
    if len(o3q) > 4096:
        sixz5 = re.sub(r"[a-zA-Z]", "0", sixz5)
    if len(yetsn) > 4096:
        sixz5 = "".join(reversed(sixz5))
    for idx in range(len(yetsn) - 1, -1, -1):
        ycba = yetsn[idx]
        if ycba == "b":
            try:
                sixz5 = _b64_to_latin1(sixz5)
            except (ValueError, binascii.Error, UnicodeDecodeError):
                return None
        elif ycba == "v":
            sixz5 = "".join(reversed(sixz5))
        else:
            tlz = (26 - ((ord(ycba) - 64) % 26)) % 26
            sixz5 = _rot_decode(sixz5, tlz)
    if len(arr) > 100000:
        try:
            sixz5 = _b64_to_latin1(sixz5)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            sixz5 = "".join(sixz5)
    q2f = len(sixz5)
    p420 = [0] * q2f
    for af865 in range(q2f - 1, 0, -1):
        nbzvj = (nbzvj * 75 + 74) % 65537
        p420[af865] = nbzvj % (af865 + 1)
    hi42 = list(sixz5)
    for af865 in range(1, q2f):
        v7gs = p420[af865]
        hi42[af865], hi42[v7gs] = hi42[v7gs], hi42[af865]
    sixz5 = "".join(hi42)
    sho2i = tpx
    gna_chars = []
    for ch in sixz5:
        zhe = ord(ch)
        sho2i = (sho2i + huox) % 256
        gna_chars.append(chr(zhe ^ sho2i))
        sho2i = (sho2i + zhe) % 256
    return "".join(gna_chars)


class _IframeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.src = None

    def handle_starttag(self, tag, attrs):
        if self.src is not None:
            return
        if tag.lower() != "iframe":
            return
        d = {k.lower(): v for k, v in attrs if v is not None}
        if d.get("data-src"):
            self.src = d["data-src"].strip()
        elif d.get("src"):
            self.src = d["src"].strip()


class _ScriptExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._in_script = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            self._in_script = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_script:
            self._in_script = False
            self.scripts.append("".join(self._buf))
            self._buf = []

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_h1 = False
        self.h1_is_title = False
        self.h1_data = []
        self.h1_found = None
        self.in_title = False
        self.title_data = []
        self.title_found = None

    def handle_starttag(self, tag, attrs):
        tl = tag.lower()
        if tl == "h1":
            d = {k.lower(): (v or "") for k, v in attrs}
            cls = d.get("class", "")
            if "section-title" in cls:
                self.in_h1 = True
                self.h1_is_title = True
                self.h1_data = []
        elif tl == "title":
            self.in_title = True
            self.title_data = []

    def handle_endtag(self, tag):
        tl = tag.lower()
        if tl == "h1" and self.in_h1:
            self.in_h1 = False
            if self.h1_is_title and self.h1_found is None:
                txt = "".join(self.h1_data).strip()
                txt = re.sub(r"\s+", " ", txt)
                if txt:
                    self.h1_found = txt
            self.h1_is_title = False
        elif tl == "title" and self.in_title:
            self.in_title = False
            if self.title_found is None:
                txt = "".join(self.title_data).strip()
                if txt:
                    txt = re.sub(r"\s+", " ", txt)[:80]
                    self.title_found = txt

    def handle_data(self, data):
        if self.in_h1 and self.h1_is_title:
            self.h1_data.append(data)
        if self.in_title:
            self.title_data.append(data)


class _AltParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.alts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "button":
            return
        d = {k.lower(): (v or "") for k, v in attrs}
        cls = d.get("class", "")
        if "alternative-link" not in cls:
            return
        vid = d.get("data-video", "").strip()
        # label will be captured in data, but we can store pending
        self._pending_vid = vid
        self._pending_label_parts = []
        self._in_alt = True
        # use stack to capture
        self._alt_depth = 1

    def handle_data(self, data):
        if getattr(self, "_in_alt", False):
            self._pending_label_parts.append(data)

    def handle_endtag(self, tag):
        if getattr(self, "_in_alt", False) and tag.lower() == "button":
            label = "".join(self._pending_label_parts).strip()
            vid = getattr(self, "_pending_vid", "").strip()
            if label or vid:
                self.alts.append((label if label else vid, vid))
            self._in_alt = False
            self._pending_label_parts = []
            self._pending_vid = ""


def _fetch_stream(url: str, referer=None, max_bytes: int = MAX_HTML_BYTES) -> bytes:
    _validate_url(url)
    h = dict(HEADERS)
    if referer:
        _validate_url(referer)
        h["Referer"] = referer
    attempt = 0
    while True:
        _rate_limit_wait()
        try:
            with requests.get(url, headers=h, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
                if r.status_code in (429, 503):
                    delay = _get_retry_after_seconds(r.headers, default=(2**min(attempt, 6)) + 0.5)  # type: ignore[arg-type]
                    delay = min(delay, 30.0)
                    time.sleep(delay)
                    attempt += 1
                    continue
                cl = r.headers.get("Content-Length")
                if cl is not None:
                    try:
                        if int(cl) > max_bytes:
                            raise ValueError(f"Content-Length too large: {cl}")
                    except ValueError:
                        raise
                    except (TypeError, AttributeError):
                        pass
                r.raise_for_status()
                data = b""
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        data += chunk
                        if len(data) > max_bytes:
                            raise ValueError(f"Response too large (> {max_bytes} bytes): {url[:100]}")
                return data
        except requests.exceptions.HTTPError as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None) if resp is not None else None
            if status in (429, 503):
                try:
                    headers = getattr(resp, "headers", {}) or {}
                except AttributeError:
                    headers = {}
                delay = _get_retry_after_seconds(headers, default=  # type: ignore[arg-type]
                    (2**min(attempt, 6)) + 0.5)  # type: ignore[arg-type]
                time.sleep(min(delay, 30.0))
                attempt += 1
                continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            delay = (2**min(attempt, 6)) * 0.5 + 0.2
            time.sleep(min(delay, 30.0))
            attempt += 1
            continue
        except ValueError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            if attempt >= 2:
                raise
            time.sleep(min(30.0, 0.5 * (2**min(attempt, 5))))
            attempt += 1
            continue


def fetch(url, referer=None):
    data = _fetch_stream(url, referer=referer, max_bytes=MAX_HTML_BYTES)
    try:
        text = data.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        text = data.decode("latin1", errors="replace")
    if "Just a moment" in text[:5000]:
        raise RuntimeError(f"403 Cloudflare - TR proxy required: {url}")
    return text


def fetch_bytes(url, referer=None):
    data = _fetch_stream(url, referer=referer, max_bytes=MAX_SEGMENT_BYTES)
    return data


def extract_iframe(html, base):
    truncated = _safe_truncate(html, 500_000)
    parser = _IframeParser()
    with contextlib.suppress(ValueError, TypeError, AttributeError, RuntimeError):
        parser.feed(truncated)
    src = parser.src
    if not src:
        return None
    src = html_module_unescape(src) if False else src
    # normalize src
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = urljoin(base, src)
    elif not src.startswith("http"):
        src = urljoin(base + "/", src)
    try:
        _validate_url(src)
    except ValueError:
        return None
    return src


def html_module_unescape(s: str) -> str:
    return html.unescape(s)


def _extract_o3q_yetsn(func_body: str):
    # find var "..." assignments; first two are o3q, yetsn
    vals = re.findall(r'var\s+\w+\s*=\s*"([^"]{0,200})"', func_body)
    if len(vals) >= 2:
        return vals[0], vals[1]
    # fallback: any quoted strings
    vals2 = re.findall(r'"([^"]{0,200})"', func_body)
    if len(vals2) >= 2:
        # heuristic: first long-ish is o3q (length >5), second short 1-10 is yetsn
        # yetsn is typically 4 chars like bPIJ
        # o3q is ~22 chars
        # just return first two
        return vals2[0], vals2[1]
    return None, None


def decode_via_python(iframe_html):
    truncated = _safe_truncate(iframe_html, 1_000_000)
    # extract scripts via parser to avoid ReDoS
    extractor = _ScriptExtractor()
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        extractor.feed(truncated)
    if not extractor.scripts:
        return None
    scripts = extractor.scripts
    for scr in scripts:
        if len(scr) > 200_000:
            continue
        if "function" not in scr or "var " not in scr:
            continue
        # original pattern guarded by length limit above (ReDoS mitigated via truncation)
        m = re.search(
            r"function\s+(\w+)\s*\(.*?\{.*?return\s+\w+\s*;\s*\}.*?var\s+(\w+)\s*=\s*(\w+)\s*\(\s*\[([^\]]+)\]",
            scr,
            re.DOTALL,
        )
        if not m:
            continue
        func_name = m.group(1)
        var_name = m.group(2)
        call_name = m.group(3)
        arr_content = m.group(4)
        if len(arr_content) > 8000:
            continue
        if func_name != call_name:
            continue
        if len(func_name) > 64 or len(var_name) > 64:
            continue
        try:
            func_pat = r"(function\s+" + re.escape(func_name) + r"\s*\(.*?\{.*?return\s+\w+\s*;\s*\})"
            func_match = re.search(func_pat, scr, re.DOTALL)
        except re.error:
            continue
        if not func_match:
            continue
        func_def = func_match.group(1)
        if len(func_def) > 8000:
            continue
        o3q, yetsn = _extract_o3q_yetsn(func_def)
        if not o3q or not yetsn:
            continue
        if len(o3q) > 200 or len(yetsn) > 20:
            continue
        # extract parts from arr_content
        parts = re.findall(r'"([^"]{0,500})"', arr_content)
        if not parts:
            parts = re.findall(r"'([^']{0,500})'", arr_content)
        if not parts:
            continue
        if len(parts) > 500:
            continue
        # sanitize parts: only allow base64-ish characters plus +/= and alphanum
        # original parts are base64 fragments like "mdBhQYCv"
        cleaned = []
        valid = True
        for p in parts:
            # unescape \/
            pp = p.replace(r"\/", "/").replace("\\/", "/")
            # allow only base64 chars + - _ / + =
            if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,500}", pp):
                # if contains invalid chars, skip this script candidate
                valid = False
                break
            if len(pp) > 200:
                valid = False
                break
            cleaned.append(pp)
        if not valid:
            continue
        try:
            decoded = _js_skhyr_decode(cleaned, o3q, yetsn)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError, binascii.Error):
            continue
        if decoded and decoded.strip().startswith("http") and "master" in decoded:
            try:
                _validate_url(decoded.strip())
                return decoded.strip()
            except ValueError:
                continue
    return None


# keep old name for compatibility but now python
def decode_via_node(iframe_html):
    return decode_via_python(iframe_html)


def find_master(html):
    truncated = _safe_truncate(html, 500_000)
    u = decode_via_python(truncated)
    if u and "master" in u:
        try:
            _validate_url(u)
            return u
        except ValueError:
            pass
    # contentUrl fallback - bounded
    m = re.search(r'"contentUrl"\s*:\s*"([^"]{0,500})"', truncated)
    if m and "master" in m.group(1):
        cand = m.group(1).replace("\\/", "/")
        try:
            _validate_url(cand)
            return cand
        except ValueError:
            pass
    m = re.search(r"https?://[^\s\"'<>]{0,500}master\.[a-z]{2,10}[^\s\"'<>]{0,200}", truncated)
    if m:
        cand = m.group(0)
        try:
            _validate_url(cand)
            return cand
        except ValueError:
            pass
    return None


def parse_master(txt, base):
    truncated = _safe_truncate(txt, 500_000)
    audios = []
    # parse line by line to avoid catastrophic backtracking
    for line in truncated.splitlines():
        if "EXT-X-MEDIA" in line and "TYPE=AUDIO" in line:
            # extract NAME and URI via simple searches, not heavy regex
            name_m = re.search(r'NAME="([^"]{0,100})"', line)
            uri_m = re.search(r'URI="([^"]{0,500})"', line)
            if name_m and uri_m:
                name = name_m.group(1)
                uri = uri_m.group(1)
                try:
                    full = urljoin(base, uri)
                    _validate_url(full)
                    audios.append((name, full))
                except ValueError:
                    continue
    video = None
    # find stream inf
    lines = truncated.splitlines()
    for i, line in enumerate(lines):
        if "EXT-X-STREAM-INF" in line and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("#"):
                try:
                    full = urljoin(base, nxt)
                    _validate_url(full)
                    video = full
                    break
                except ValueError:
                    continue
    return video, audios


def parse_segments(txt, base):
    truncated = _safe_truncate(txt, 1_000_000)
    segs = []
    _validate_url(base)  # base must be valid
    for line in truncated.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > 2048:
            continue
        if line.startswith("http"):
            try:
                _validate_url(line)
                segs.append(line)
            except ValueError:
                continue
        else:
            try:
                full = urljoin(base, line)
                _validate_url(full)
                segs.append(full)
            except ValueError:
                continue
    return segs


def _sanitize_output_path(out: str) -> str:
    if not out or not isinstance(out, str):
        raise ValueError("Output path cannot be empty")
    out = out.strip().strip('"').strip("'")
    if not out:
        raise ValueError("Output path is empty")
    if "\x00" in out or "\n" in out or "\r" in out:
        raise ValueError("Output path contains invalid characters")
    if len(out) > 255:
        raise ValueError("Output path too long")
    p = Path(out).expanduser()  # lgtm[py/path-injection]  # codeql[py/path-injection] - output path is sanitized via _sanitize_output_path, user-controlled output is intentional
    # prevent ffmpeg option injection: if output starts with '-', prefix with ./
    p_str = str(p)
    if p_str.startswith("-"):
        # use string prefix to keep ./, Path would normalize it away
        p_str = "./" + p_str.lstrip("./")
        p = Path(p_str)
    # normalize but allow absolute paths inside user's writable area
    # ensure parent exists
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Failed to create output directory: {e}") from e
    if p.is_dir():
        raise ValueError("Output path cannot be a directory")
    # return with ./ prefix preserved if needed
    if p_str.startswith("./"):
        return p_str
    return str(p)


def _escape_concat_path(p: Path) -> str:
    # ffmpeg concat demuxer escaping: single quote -> '\'' ; use posix
    s = p.as_posix()
    return s.replace("'", "'\\''")


def download_parallel(urls, referer, tmpdir, prefix, limit=None):
    if not urls:
        raise ValueError(f"{prefix} URL list is empty")
    if limit is not None:
        try:
            limit = int(limit)
            if limit <= 0:
                limit = None
            else:
                urls = urls[:limit]
        except (ValueError, TypeError):
            pass
    if len(urls) > 5000:
        print(f"[!] Warning: {len(urls)} too many segments, limited to first 5000")
        urls = urls[:5000]
    tmp_path = Path(tmpdir)
    # tmpdir must be inside tempfile, but validate
    try:
        tmp_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Failed to create tmpdir: {e}") from e
    # ensure tmp_path is a directory and not symlink to sensitive
    try:
        resolved = tmp_path.resolve()
        if not resolved.is_dir():
            raise ValueError
    except OSError:
        raise ValueError(f"Invalid tmpdir: {tmpdir}")

    print(f"[*] {prefix} {len(urls)} downloading segments (16 thread)...")
    files: list[Path | None] = [None] * len(urls)

    def dl(idx_url):
        idx, url = idx_url
        _validate_url(url)
        attempt = 0
        while True:
            try:
                data = fetch_bytes(url, referer=referer)
                # ensure idx is int and prefix safe
                safe_prefix = re.sub(r"\W+", "_", prefix)[:20]
                p = tmp_path / f"{safe_prefix}_{idx:05d}.ts"
                # ensure p is inside tmp_path
                try:
                    if not p.resolve().is_relative_to(resolved):
                        # fallback to is_relative_to emulation for <3.9
                        if str(p.resolve()).startswith(str(resolved)):
                            pass
                        else:
                            raise ValueError("Path traversal")
                except AttributeError:
                    # Python <3.9
                    if not str(p.resolve()).startswith(str(resolved)):
                        raise ValueError("Path traversal")
                with open(p, "wb") as f:
                    f.write(data)
                return idx, p
            except ValueError:
                raise
            except KeyboardInterrupt:
                raise
            except (requests.exceptions.RequestException, OSError, RuntimeError) as e:
                status = None
                try:
                    resp = getattr(e, "response", None)
                    if resp is not None:
                        status = getattr(resp, "status_code", None)
                except AttributeError:
                    status = None
                if status in (429, 503):
                    try:
                        headers = getattr(resp, "headers", {}) or {}  # type: ignore[union-attr]
                    except AttributeError:
                        headers = {}
                    delay = _get_retry_after_seconds(headers, default=  # type: ignore[arg-type]
                    (2**min(attempt, 6)) + 0.5)  # type: ignore[arg-type]
                    time.sleep(min(delay, 30.0))
                    attempt += 1
                    continue
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                    time.sleep(min(30.0, (2**min(attempt, 6)) * 0.5 + 0.2))
                    attempt += 1
                    continue
                msg = str(e).lower()
                if "429" in msg or "503" in msg or "too many requests" in msg or "rate limit" in msg:
                    time.sleep(min(30.0, (2**min(attempt, 6)) * 0.5 + 0.5))
                    attempt += 1
                    continue
                if attempt >= 2:
                    raise
                time.sleep(min(30.0, (2**min(attempt, 6)) * 0.5 + 0.5))
                attempt += 1
                continue

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(dl, (i, u)): i for i, u in enumerate(urls)}
        done = 0
        for fut in as_completed(futs):
            try:
                idx, p = fut.result()
                files[idx] = p  # type: ignore[index]
                done += 1
                if done % 100 == 0 or done == len(urls):
                    print(f"  {done}/{len(urls)}")
            except (OSError, ValueError, RuntimeError, requests.exceptions.RequestException) as e:
                print(f"[!] segment error: {e}")

    list_path = tmp_path / f"{re.sub(r'\W+', '_', prefix)[:20]}_list.txt"
    # validate list_path inside tmp
    try:
        if not list_path.resolve().is_relative_to(resolved):
            raise ValueError
    except AttributeError:
        if not str(list_path.resolve()).startswith(str(resolved)):
            raise ValueError("list_path traversal")
    except OSError:
        # fallback for resolve errors, continue to use list_path
        _ = 0
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            if p:
                # we use absolute but escaped
                abs_p = Path(p).resolve()
                # ensure still inside tmp
                try:
                    if not abs_p.is_relative_to(resolved):
                        continue
                except AttributeError:
                    if not str(abs_p).startswith(str(resolved)):
                        continue
                except OSError:
                    continue
                esc = _escape_concat_path(abs_p)
                f.write(f"file '{esc}'\n")
    return str(list_path), files


def extract_title(html):
    truncated = _safe_truncate(html, 500_000)
    parser = _TitleParser()
    try:
        parser.feed(truncated)
    except (ValueError, AttributeError, TypeError):
        pass
    if parser.h1_found:
        return parser.h1_found
    if parser.title_found:
        return parser.title_found
    return "Unknown Film"


def extract_alternatives(html, base_iframe):
    truncated = _safe_truncate(html, 500_000)
    parser = _AltParser()
    try:
        parser.feed(truncated)
    except (ValueError, AttributeError, TypeError):
        pass
    alts = [(label.strip() if label else "Default", vid.strip()) for label, vid in parser.alts if vid is not None]
    # filter empty
    alts = [(n, v) for n, v in alts if n]
    if not alts:
        # fallback regex bounded for compatibility if parser missed due to malformed html
        # use safe bounded regex
        for m in re.finditer(r'class="alternative-link"[^>]{0,500}data-video="([^"]{0,200})"[^>]{0,500}>([^<]{0,100})</button>', truncated):
            vid, name = m.group(1).strip(), m.group(2).strip()
            if len(vid) > 200 or len(name) > 100:
                continue
            alts.append((name, vid))
    if not alts:
        alts = [("Default", "")]
    return alts


def build_iframe_url(base_iframe, alt_vid, alt_name):
    try:
        _validate_url(base_iframe)
    except ValueError:
        return base_iframe
    if not alt_vid or alt_name.lower() == "default":
        return base_iframe
    if len(alt_vid) > 200:
        return base_iframe
    m = re.search(r"/embed/([^/?]{1,100})", base_iframe)
    vid = m.group(1) if m else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", vid):
        return base_iframe
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", alt_vid) and not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", alt_vid):
        return base_iframe
    if "rapidrame" in alt_name.lower():
        cand = f"https://hdfilmcehennemi.mobi/video/embed/{vid}/?rapidrame_id={alt_vid}"
    else:
        cand = f"https://hdfilmcehennemi.mobi/video/embed/{vid}/"
    try:
        _validate_url(cand)
        return cand
    except ValueError:
        return base_iframe


def extract_subtitles(iframe_html):
    truncated = _safe_truncate(iframe_html, 500_000)
    subs = []
    # bounded pattern
    for m in re.finditer(r'"file"\s*:\s*"(https:[^"]{0,500}\.vtt)"[^}]{0,500}"label"\s*:\s*"([^"]{0,100})"', truncated):
        url, label = m.group(1).replace("\\/", "/"), m.group(2)
        try:
            _validate_url(url)
            subs.append((label, url))
        except ValueError:
            continue
    if not subs:
        for m in re.finditer(r'"file":"(https:[^"]{0,500}\.vtt)"', truncated):
            url = m.group(1).replace("\\/", "/")
            try:
                _validate_url(url)
                subs.append(("Unknown", url))
            except ValueError:
                continue
    return subs


def choose_interactive(options, prompt, default=1):
    if not options:
        return None
    if len(options) > 100:
        options = options[:100]
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        disp = opt[0] if isinstance(opt, tuple) else str(opt)
        if len(disp) > 200:
            disp = disp[:200]
        print(f"  [{i}] {disp}")
    while True:
        try:
            ans = input(f"Selection [1-{len(options)}] (default {default}): ").strip()
            if ans == "":
                ans = str(default)
            if not ans.isdigit():
                print("Enter a number")
                continue
            idx = int(ans)
            if 1 <= idx <= len(options):
                return options[idx - 1]
            print("Invalid number")
        except ValueError:
            print("Enter a number")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled")
            sys.exit(0)


def main():
    global RATE_LIMIT_RPS
    import argparse

    ap = argparse.ArgumentParser(description="HDFilmCehennemi downloader")
    ap.add_argument("url", nargs="?", help="Film URL")
    ap.add_argument("-o", "--output", help="Output mp4")
    ap.add_argument("--limit", type=int, help="Segment limit for testing")
    ap.add_argument("-i", "--interactive", action="store_true", help="Interactive selection (quality/audio/subtitle/source)")
    ap.add_argument("--rate-limit", type=int, default=8, help="Max requests per second for all workers (default 8)")
    ap.add_argument("--debug", action="store_true", help="Write debug file")
    args = ap.parse_args()

    if args.rate_limit is not None:
        try:
            rl = int(args.rate_limit)
            if 1 <= rl <= 100:
                RATE_LIMIT_RPS = rl
            else:
                print(f"Invalid --rate-limit {rl}, using default {RATE_LIMIT_RPS}")
        except (ValueError, TypeError):
            pass

    film_url = args.url
    out = args.output
    limit = args.limit
    debug = args.debug
    interactive = args.interactive

    # url yoksa interactivete sor
    if not film_url and interactive:
        try:
            film_url = input("Film URL (example: https://www.hdfilmcehennemi.nl/hd-asiklar-sehri-7/): ").strip().strip('"').strip("'")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled")
            sys.exit(0)
        if not film_url:
            print("URL is required")
            sys.exit(1)
    if not film_url:
        print('Usage: python3 hdfilmcehennemi.py "URL" [-o output.mp4] [--limit N] [-i]')
        sys.exit(1)

    try:
        _validate_url(film_url)
    except ValueError as e:
        print(f"[-] Invalid film URL: {e}")
        sys.exit(1)

    parsed_film = urlparse(film_url)
    if parsed_film.scheme not in ("http", "https"):
        print("[-] Film URL must be http/https")
        sys.exit(1)

    # output path handling
    if not out and not interactive:
        slug = parsed_film.path.strip("/").split("/")[-1] or "film"
        slug = re.sub(r'[\\/:*?"<>|]', "_", slug)[:100]
        if not slug:
            slug = "film"
        out = slug + ".mp4"
        try:
            out = _sanitize_output_path(out)
        except ValueError as e:
            print(f"[-] Output path error: {e}")
            sys.exit(1)
    elif out:
        try:
            out = _sanitize_output_path(out)
        except ValueError as e:
            print(f"[-] Output path error: {e}")
            sys.exit(1)
    # interactive ise out sonradan sorulacak

    print(f"[*] Film: {film_url}")
    base = f"{parsed_film.scheme}://{parsed_film.netloc}"
    print("[*] Fetching page...")
    try:
        html_content = fetch(film_url)
    except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
        print(f"[-] Failed to fetch page: {e}")
        sys.exit(1)
    title = extract_title(html_content)
    print(f"[+] Title: {title}")

    base_iframe = extract_iframe(html_content, base)
    if not base_iframe:
        print("[-] iframe yok")
        sys.exit(1)
    print(f"[+] iframe (raw): {base_iframe}")

    alts = extract_alternatives(html_content, base_iframe)
    if interactive and len(alts) > 1:
        chosen = choose_interactive(
            [(f"{n} ({'active' if i == 0 else 'alternative'})", (n, v)) for i, (n, v) in enumerate(alts)],
            "Select video source:",
            default=1,
        )
        if chosen is None:
            print("[-] No source selected")
            sys.exit(1)
        alt_name, alt_vid = chosen[1]
        iframe = build_iframe_url(base_iframe, alt_vid, alt_name)
        print(f"[+] Selected source: {alt_name} -> {iframe}")
    else:
        iframe = base_iframe
        alt_name = alts[0][0] if alts else "Default"

    print("[*] Fetching iframe...")
    try:
        iframe_html = fetch(iframe, referer=film_url)
    except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
        print(f"[-] Failed to fetch iframe: {e}")
        sys.exit(1)
    print("[*] Resolving master...")
    master = find_master(iframe_html)
    if not master:
        print("[-] master not found")
        if debug:
            dbg = Path(tempfile.gettempdir()) / "iframe_debug.html"
            try:
                with open(dbg, "w", encoding="utf-8") as f:
                    f.write(iframe_html[:1_000_000])
                print(f"[*] debug written: {dbg}")
            except OSError:
                print("[!] Failed to write debug file")
        sys.exit(1)
    print(f"[+] master: {master}")

    referer = "https://hdfilmcehennemi.mobi/"
    print("[*] Fetching master content...")
    try:
        master_txt = fetch(master, referer=referer)
    except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
        print(f"[-] Failed to fetch master: {e}")
        sys.exit(1)
    print(master_txt[:500])
    try:
        mbase = master.rsplit("/", 1)[0] + "/"
        _validate_url(mbase)
    except ValueError:
        print("[-] master base URL invalid")
        sys.exit(1)

    streams = []
    truncated_master = _safe_truncate(master_txt, 500_000)
    for line in truncated_master.splitlines():
        if "EXT-X-STREAM-INF" in line:
            # handled via parse_master style, but keep old logic for streams
            pass
    # parse streams via line iteration
    lines = truncated_master.splitlines()
    for i, line in enumerate(lines):
        if "#EXT-X-STREAM-INF:" in line and i + 1 < len(lines):
            attrs = line
            uri = lines[i + 1].strip()
            if not uri or uri.startswith("#") or len(uri) > 2048:
                continue
            # bandwidth / resolution parse
            bw_m = re.search(r"BANDWIDTH=(\d{1,10})", attrs)
            res_m = re.search(r"RESOLUTION=\d+x(\d{1,5})", attrs)
            try:
                full = urljoin(mbase, uri)
                _validate_url(full)
            except ValueError:
                continue
            bw_str = f"{res_m.group(1)}p" if res_m else "Unknown"
            bw_kbps = f"{int(bw_m.group(1))//1000}kbps" if bw_m else uri[:30]
            label = f"{bw_str} - {bw_kbps}" if bw_m else uri
            streams.append((label, full, attrs))

    video_url = None
    audios = []
    if streams:
        print(f"[+] {len(streams)} qualities found")
        if interactive:
            chosen_stream = choose_interactive(
                [(d, (d, u)) for d, u, _ in streams],
                "Select quality:",
                default=1,
            )
            if chosen_stream is None:
                print("[-] No quality selected")
                sys.exit(1)
            video_url = chosen_stream[1][1]
            print(f"[+] Selected quality: {chosen_stream[0]} -> {video_url}")
        else:
            def bw_key(x):
                mm = re.search(r"BANDWIDTH=(\d+)", x[2])
                return int(mm.group(1)) if mm else 0

            streams_sorted = sorted(streams, key=bw_key, reverse=True)
            video_url = streams_sorted[0][1]
            print(f"[+] Auto selected highest: {video_url}")
        for line in truncated_master.splitlines():
            if "EXT-X-MEDIA" in line and "TYPE=AUDIO" in line:
                name_m = re.search(r'NAME="([^"]{0,100})"', line)
                uri_m = re.search(r'URI="([^"]{0,500})"', line)
                if name_m and uri_m:
                    try:
                        full = urljoin(mbase, uri_m.group(1))
                        _validate_url(full)
                        audios.append((name_m.group(1), full))
                    except ValueError:
                        continue
        if audios:
            print(f"[+] audios: {audios}")
            if interactive:
                opts = [(f"{n}", (n, u)) for n, u in audios]
                chosen_aud = choose_interactive(opts, "Select audio language:", default=1)
                if chosen_aud is None:
                    print("[-] No audio selected")
                    sys.exit(1)
                audios = [chosen_aud[1]]
                print(f"[+] Selected audio: {chosen_aud[0]}")
    else:
        video_url, audios = parse_master(master_txt, mbase)
        print(f"[+] video: {video_url}")
        print(f"[+] audios: {audios}")
        if interactive and audios:
            opts = [(f"{n}", (n, u)) for n, u in audios]
            chosen_aud = choose_interactive(opts, "Select audio language:", default=1)
            if chosen_aud is None:
                print("[-] No audio selected")
                sys.exit(1)
            audios = [chosen_aud[1]]
            print(f"[+] Selected audio: {chosen_aud[0]}")

    if not video_url:
        print("[-] video url missing")
        sys.exit(1)
    try:
        _validate_url(video_url)
    except ValueError as e:
        print(f"[-] video URL invalid: {e}")
        sys.exit(1)

    # subtitles
    subs = extract_subtitles(iframe_html)
    chosen_subs = subs
    if interactive and subs:
        print(f"\n[+] {len(subs)} subtitles found:")
        for i, (label, url) in enumerate(subs, 1):
            print(f"  [{i}] {label} -> {url[:60]}")
        try:
            ans = input("Download subtitles? [Y/n] (y: all, n: none, 1,3 for selection): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled")
            sys.exit(0)
        # safe: length limit, only allowed characters
        if len(ans) > 200:
            ans = ans[:200]
        if ans in ["y", "yes", "all", "e", "evet", "hepsi", ""]:
            chosen_subs = subs
        elif ans in ["n", "no", "h", "hayir", "hayır"]:
            chosen_subs = []
        else:
            # 1,3 style selection - only digits, comma, space allowed
            if not re.fullmatch(r"[0-9,\s]{1,100}", ans):
                print("[!] Invalid selection, none will be downloaded")
                chosen_subs = []
            else:
                try:
                    idxs = [int(x) - 1 for x in re.split(r"[,\s]+", ans) if x.strip().isdigit()]
                    # limit
                    idxs = [i for i in idxs if 0 <= i < len(subs)]
                    chosen_subs = [subs[i] for i in idxs]
                except (ValueError, AttributeError):
                    chosen_subs = []

    # interactive output/limit sor
    if interactive:
        if not out:
            defslug = urlparse(film_url).path.strip("/").split("/")[-1] or "film"
            defslug = re.sub(r'[\\/:*?"<>|]', "_", defslug)[:100]
            if not defslug:
                defslug = "film"
            defslug = defslug + ".mp4"
            while True:
                try:
                    ans = input(f"Output file [{defslug}]: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nCancelled")
                    sys.exit(0)
                if len(ans) > 255:
                    ans = ans[:255]
                ans = ans.strip().strip('"').strip("'")
                raw_out = ans if ans else defslug
                try:
                    out = _sanitize_output_path(raw_out)
                    break
                except ValueError as e:
                    print(f"[-] Output path error: {e}, please try again")
        if limit is None:
            try:
                ans = input("Segment limit? (empty: all, e.g. 20 for test): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled")
                sys.exit(0)
            if ans.isdigit():
                # 1-5000 between limit
                val = int(ans)
                if 1 <= val <= 5000:
                    limit = val
                else:
                    print("[!] Limit must be between 1 and 5000, all will be downloaded")

    print("[*] Fetching video playlist...")
    try:
        video_txt = fetch(video_url, referer=referer)
    except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
        print(f"[-] failed to fetch video playlist: {e}")
        sys.exit(1)
    try:
        vbase = video_url.rsplit("/", 1)[0] + "/"
        _validate_url(vbase)
    except ValueError:
        print("[-] video base invalid")
        sys.exit(1)
    v_segs = parse_segments(video_txt, vbase)
    print(f"[+] video segments: {len(v_segs)}")
    if not v_segs:
        print("[-] no video segments")
        sys.exit(1)

    a_segs = None
    audio_url = None
    if audios:
        audio_url = audios[0][1] if isinstance(audios[0], tuple) else audios[0]
        try:
            _validate_url(audio_url)
        except ValueError:
            audio_url = None
            audios = []
        if audio_url:
            print("[*] Fetching audio playlist...")
            try:
                audio_txt = fetch(audio_url, referer=referer)
            except (ValueError, RuntimeError, requests.exceptions.RequestException) as e:
                print(f"[-] failed to fetch audio playlist: {e}")
                audio_url = None
                a_segs = None
            else:
                try:
                    abase = audio_url.rsplit("/", 1)[0] + "/"
                    _validate_url(abase)
                    a_segs = parse_segments(audio_txt, abase)
                    print(f"[+] audio segments: {len(a_segs)}")
                    if not a_segs:
                        a_segs = None
                except ValueError:
                    a_segs = None

    tmpdir = tempfile.mkdtemp(prefix="hdf_")
    try:
        v_list, _ = download_parallel(v_segs, referer, tmpdir, "video", limit=limit)
        a_list = None
        if a_segs:
            a_list, _ = download_parallel(a_segs, referer, tmpdir, "audio", limit=limit)

        print(f"[*] Merging -> {out}")
        # ensure ffmpeg exists
        if shutil.which("ffmpeg") is None:
            print("[-] ffmpeg not found")
            sys.exit(1)
        # sanitize out already, ensure not starting with -
        out_path = Path(out)
        if out_path.name.startswith("-"):
            out = str(Path(".") / out_path)
            out_path = Path(out)
        # build cmd with list, no shell
        if a_segs and a_list:
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-f", "concat", "-safe", "0", "-i", a_list, "-c", "copy", str(out_path)]
        else:
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", v_list, "-c", "copy", str(out_path)]
        # log without exposing full paths if needed
        print(" ".join(cmd))
        ret = subprocess.run(cmd, shell=False, check=False)  # lgtm[py/command-line-injection]  # codeql[py/command-line-injection] - out_path sanitized via _sanitize_output_path, shell=False
        if ret.returncode == 0:
            try:
                size = os.path.getsize(out_path)  # lgtm[py/path-injection]  # codeql[py/path-injection] - out_path is sanitized output path
            except OSError:
                size = 0
            print(f"[+] Done: {out_path} ({size} byte)")
            if chosen_subs:
                print(f"[*] {len(chosen_subs)} downloading subtitles...")
                for label, url in chosen_subs:
                    try:
                        _validate_url(url)
                        data = fetch(url, referer=iframe)
                        safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", label)[:20]
                        if not safe_label:
                            safe_label = "subtitle"
                        safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", out_path.stem)[:100]
                        if not safe_stem:
                            safe_stem = "output"
                        sub_path = out_path.parent / f"{safe_stem}.{safe_label}.vtt"
                        # validate sub_path is in same dir or subdir of out_path parent
                        try:
                            # allow same parent
                            if not str(sub_path.resolve()).startswith(str(out_path.parent.resolve())):
                                # fallback to out parent + safe name
                                sub_path = out_path.parent / f"{out_path.stem}.{safe_label}.vtt"
                        except OSError:
                            sub_path = out_path.parent / f"{out_path.stem}.{safe_label}.vtt"
                        with open(sub_path, "w", encoding="utf-8") as sf:
                            sf.write(data)
                        print(f"  [+] {label}: {sub_path}")
                    except (OSError, ValueError, UnicodeError, requests.exceptions.RequestException) as e:
                        print(f"  [!] {label} error: {e}")
        else:
            print("[-] ffmpeg error")
            sys.exit(1)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
