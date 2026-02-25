#!/usr/bin/env python3
"""
yaml_to_podcast_rss.py

Generate a podcast RSS feed from a human-friendly YAML file.

Usage:
  python yaml_to_podcast_rss.py feed.yaml
  python yaml_to_podcast_rss.py feed.yaml --out feed.xml
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import yaml
from dateutil import parser as dateparser


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("podcast", PODCAST_NS)


def _text(el: ET.Element, tag: str, value: Optional[Any], ns: Optional[str] = None) -> Optional[ET.Element]:
    """Add a subelement with text if value is not None/empty."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    qtag = f"{{{ns}}}{tag}" if ns else tag
    child = ET.SubElement(el, qtag)
    child.text = s
    return child


def _bool_text(value: Any) -> str:
    """Podcast feeds usually want 'yes'/'no' for explicit; PodcastIndex uses 'yes'/'no' in several tags too."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    s = str(value).strip().lower()
    if s in {"yes", "true", "1"}:
        return "yes"
    if s in {"no", "false", "0"}:
        return "no"
    # default to 'no' if ambiguous
    return "no"


def _parse_date(value: Any) -> dt.datetime:
    """
    Parse date-ish strings into an aware datetime if possible; falls back to naive UTC.
    Accepts ISO 8601, 'YYYY-MM-DD', etc.
    """
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, dt.date):
        d = dt.datetime(value.year, value.month, value.day)
    else:
        d = dateparser.parse(str(value))
        if d is None:
            raise ValueError(f"Could not parse date: {value!r}")
    if d.tzinfo is None:
        # Assume UTC if no tz provided
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _rfc2822(value: Any) -> str:
    """Convert a date to RFC 2822 string (RSS pubDate)."""
    d = _parse_date(value)
    return format_datetime(d)


def _indent(elem: ET.Element, level: int = 0) -> None:
    """Pretty-print indentation for ElementTree."""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for child in elem:
            _indent(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def _require(d: Dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ValueError(f"Missing required field '{key}' in {where}")
    return d[key]


def _normalize_category(cat: Any) -> Tuple[str, Optional[str]]:
    """
    Allow either:
      - "Technology"
      - ["Education", "How To"]
    """
    if isinstance(cat, (list, tuple)) and len(cat) >= 1:
        top = str(cat[0]).strip()
        sub = str(cat[1]).strip() if len(cat) > 1 and cat[1] is not None else None
        return top, sub
    return str(cat).strip(), None


def _sanitize_guid_fallback(s: str) -> str:
    # If no guid provided, use enclosure URL; make sure it's not empty.
    s = s.strip()
    if not s:
        raise ValueError("Cannot derive guid from empty enclosure url")
    return s


def build_rss(feed: Dict[str, Any], out_path: str) -> None:
    channel_cfg = _require(feed, "channel", "root")
    episodes = feed.get("episodes", [])
    if not isinstance(episodes, list):
        raise ValueError("'episodes' must be a list")

    title = _require(channel_cfg, "title", "channel")
    link = _require(channel_cfg, "link", "channel")
    description = _require(channel_cfg, "description", "channel")

    base_url = channel_cfg.get("base_url", "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    rss = ET.Element("rss", {"version": "2.0"})
    rss.set(f"xmlns:itunes", ITUNES_NS)
    rss.set(f"xmlns:atom", ATOM_NS)
    rss.set(f"xmlns:podcast", PODCAST_NS)

    channel = ET.SubElement(rss, "channel")

    # Atom self link (recommended)
    # If you know the final public URL of your feed, set channel.feed_url
    feed_url = channel_cfg.get("feed_url")
    if feed_url:
        ET.SubElement(channel, f"{{{ATOM_NS}}}link", {
            "href": str(feed_url),
            "rel": "self",
            "type": "application/rss+xml",
        })

    _text(channel, "title", title)
    _text(channel, "link", link)
    _text(channel, "description", description)
    _text(channel, "language", channel_cfg.get("language", "en-us"))

    # Basic RSS author/contact (non-standard in RSS2, but many still include)
    _text(channel, "managingEditor", channel_cfg.get("managingEditor"))
    _text(channel, "webMaster", channel_cfg.get("webMaster"))

    # iTunes channel fields
    _text(channel, "author", channel_cfg.get("author"), ns=ITUNES_NS)
    _text(channel, "summary", channel_cfg.get("summary") or description, ns=ITUNES_NS)
    _text(channel, "subtitle", channel_cfg.get("subtitle"), ns=ITUNES_NS)
    _text(channel, "type", channel_cfg.get("itunes_type"), ns=ITUNES_NS)  # episodic|serial (optional)

    explicit_val = channel_cfg.get("explicit")
    if explicit_val is not None:
        _text(channel, "explicit", _bool_text(explicit_val), ns=ITUNES_NS)

    owner = channel_cfg.get("owner") or {}
    if isinstance(owner, dict) and (owner.get("name") or owner.get("email")):
        owner_el = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
        _text(owner_el, "name", owner.get("name"), ns=ITUNES_NS)
        _text(owner_el, "email", owner.get("email"), ns=ITUNES_NS)

    # Artwork
    image = channel_cfg.get("image")
    if image:
        ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": str(image).strip()})
        # RSS <image> is optional; include for older clients
        img_el = ET.SubElement(channel, "image")
        _text(img_el, "url", image)
        _text(img_el, "title", title)
        _text(img_el, "link", link)

    # Categories
    for cat in (channel_cfg.get("categories") or []):
        top, sub = _normalize_category(cat)
        if not top:
            continue
        cat_el = ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": top})
        if sub:
            ET.SubElement(cat_el, f"{{{ITUNES_NS}}}category", {"text": sub})

    # PodcastIndex extras (optional)
    locked = channel_cfg.get("locked")
    if locked is not None:
        _text(channel, "locked", _bool_text(locked), ns=PODCAST_NS)

    funding = channel_cfg.get("funding")
    if isinstance(funding, dict) and funding.get("url"):
        f_el = ET.SubElement(channel, f"{{{PODCAST_NS}}}funding", {"url": str(funding["url"]).strip()})
        if funding.get("message"):
            f_el.text = str(funding["message"]).strip()

    # Items
    for ep in episodes:
        if not isinstance(ep, dict):
            raise ValueError("Each episode must be a mapping/object")

        item = ET.SubElement(channel, "item")
        _text(item, "title", _require(ep, "title", "episode"))

        # description can be plain text; if you want HTML, put it here and consider wrapping CDATA (not done by ElementTree)
        _text(item, "description", ep.get("description") or "")

        # pubDate (recommended)
        pub = ep.get("pubDate") or ep.get("date")
        if pub:
            _text(item, "pubDate", _rfc2822(pub))

        # Link (optional) - per episode web page
        if ep.get("link"):
            _text(item, "link", ep.get("link"))

        # GUID
        enclosure_cfg = _require(ep, "enclosure", "episode")
        if not isinstance(enclosure_cfg, dict):
            raise ValueError("episode.enclosure must be a mapping/object")

        enc_url = _require(enclosure_cfg, "url", "episode.enclosure")
        enc_type = enclosure_cfg.get("type", "audio/mpeg")
        enc_len = enclosure_cfg.get("length")

        # If enc_url is a filename and base_url exists, join them
        if base_url and not re.match(r"^https?://", str(enc_url).strip(), re.IGNORECASE):
            enc_url_full = urljoin(base_url, str(enc_url).lstrip("/"))
        else:
            enc_url_full = str(enc_url).strip()

        enc_attrib = {"url": enc_url_full, "type": str(enc_type).strip()}
        if enc_len is not None:
            enc_attrib["length"] = str(int(enc_len))
        ET.SubElement(item, "enclosure", enc_attrib)

        guid = ep.get("guid")
        guid_val = str(guid).strip() if guid else _sanitize_guid_fallback(enc_url_full)
        guid_el = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid_el.text = guid_val

        # iTunes episode fields
        _text(item, "author", ep.get("author") or channel_cfg.get("author"), ns=ITUNES_NS)
        _text(item, "summary", ep.get("summary") or ep.get("description"), ns=ITUNES_NS)
        if ep.get("subtitle"):
            _text(item, "subtitle", ep.get("subtitle"), ns=ITUNES_NS)

        if ep.get("duration"):
            _text(item, "duration", ep.get("duration"), ns=ITUNES_NS)

        if ep.get("explicit") is not None:
            _text(item, "explicit", _bool_text(ep.get("explicit")), ns=ITUNES_NS)

        if ep.get("image"):
            ET.SubElement(item, f"{{{ITUNES_NS}}}image", {"href": str(ep["image"]).strip()})

        if ep.get("season") is not None:
            _text(item, "season", ep.get("season"), ns=ITUNES_NS)

        if ep.get("episode") is not None:
            _text(item, "episode", ep.get("episode"), ns=ITUNES_NS)

        if ep.get("episodeType"):
            _text(item, "episodeType", ep.get("episodeType"), ns=ITUNES_NS)

        # Optional: content:encoded, transcripts, chapters, etc. can be added later.

    _indent(rss)
    tree = ET.ElementTree(rss)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml_path", help="Path to feed.yaml")
    ap.add_argument("--out", help="Override output path (otherwise uses channel.output or feed.xml)")
    args = ap.parse_args(argv)

    yaml_path = args.yaml_path
    if not os.path.exists(yaml_path):
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 2

    with open(yaml_path, "r", encoding="utf-8") as f:
        feed = yaml.safe_load(f)

    if not isinstance(feed, dict):
        print("Root YAML must be a mapping/object", file=sys.stderr)
        return 2

    out_path = args.out or (feed.get("channel", {}) or {}).get("output") or "feed.xml"

    try:
        build_rss(feed, out_path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))