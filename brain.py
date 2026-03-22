"""Brain dump parser — stream-of-consciousness text to structured workstreams.

Uses heuristic NLP to split freeform text into discrete tasks with
reasonable names, categories, and statuses. No external API required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import Category, Workstream


# Patterns that suggest splitting points
SPLIT_PATTERNS = [
    r",\s*(?:and\s+)?(?:also|plus|oh\s+and|don'?t\s+forget)\s+",
    r"\.\s+(?:Also|Plus|Oh|And|Don'?t\s+forget|I\s+(?:also\s+)?need)\s+",
    r";\s+",
    r",\s+and\s+",
    r"\band\s+(?:also|then)\s+",
    r"\balso\b\s+",
]

# Words that indicate blocked status
BLOCKED_KEYWORDS = [
    r"\bblocked\b", r"\bwaiting\s+(?:on|for)\b", r"\bstuck\b",
    r"\bcan'?t\s+(?:do|start|continue)\b", r"\bdepends\s+on\b",
    r"\bblocking\b", r"\bblocked\s+on\b",
]

# Words that indicate review status
REVIEW_KEYWORDS = [
    r"\breview\b", r"\bPR\b", r"\bMR\b", r"\bpull\s+request\b",
    r"\bmerge\s+request\b", r"\bcode\s+review\b", r"\breview\b",
    r"\bcheck\s+(?:on|over)\b",
]

# Words that indicate in-progress
PROGRESS_KEYWORDS = [
    r"\bworking\s+on\b", r"\bin\s+progress\b", r"\bstill\b",
    r"\bcontinue\b", r"\bfinish\b", r"\bwrap\s+up\b",
]

# Words that indicate done
DONE_KEYWORDS = [
    r"\bdone\b", r"\bfinished\b", r"\bcompleted?\b", r"\bshipped\b",
]

# Words that suggest work category
WORK_KEYWORDS = [
    r"\bUB-\d+\b", r"\bticket\b", r"\bjira\b", r"\bsprint\b",
    r"\bdeploy\b", r"\bprod(?:uction)?\b", r"\bstaging\b",
    r"\bPR\b", r"\bMR\b", r"\bpipeline\b", r"\bCI\b",
    r"\brelease\b", r"\bmigration\b", r"\bapi\b",
    r"\bendpoint\b", r"\bservice\b", r"\bauth\b",
    r"\bbug\b", r"\bhotfix\b", r"\bclient\b",
]

# Words that suggest meta category
META_KEYWORDS = [
    r"\btool(?:ing)?\b", r"\bsetup\b", r"\bconfig(?:ure)?\b",
    r"\bworkflow\b", r"\bautomation\b", r"\bscript\b",
    r"\bCLI\b", r"\bdashboard\b", r"\borchestrat\w*\b",
]


@dataclass
class ParsedTask:
    """A task extracted from brain dump text."""
    raw_text: str
    name: str
    category: Category


def _clean_fragment(text: str) -> str:
    """Clean up a text fragment into a usable string."""
    text = text.strip()
    # Remove leading conjunctions/filler
    text = re.sub(r"^(?:and|also|plus|oh|then|so|but|,)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:I\s+(?:need\s+to|should|have\s+to|gotta|must)|need\s+to|don'?t\s+forget\s+to?|remember\s+to)\s+", "", text, flags=re.IGNORECASE)
    # Strip trailing punctuation
    text = text.rstrip(".,;!")
    return text.strip()


def _extract_name(text: str) -> str:
    """Extract a concise name from task text."""
    clean = _clean_fragment(text)
    if not clean:
        return text.strip()[:60]

    # If it's short enough, use it as-is
    if len(clean) <= 50:
        return clean

    # Try to find a core noun phrase
    # Remove "the" from start
    name = re.sub(r"^the\s+", "", clean, flags=re.IGNORECASE)

    # Truncate at reasonable points
    for delimiter in [" - ", " — ", " because ", " since ", " so that ", " before ", " after "]:
        if delimiter in name:
            name = name[:name.index(delimiter)]
            break

    # If still too long, just truncate
    if len(name) > 50:
        name = name[:47] + "..."

    return name


def _detect_category(text: str) -> Category:
    """Detect the most likely category from text content."""
    text_lower = text.lower()
    work_score = sum(1 for pat in WORK_KEYWORDS if re.search(pat, text_lower))
    meta_score = sum(1 for pat in META_KEYWORDS if re.search(pat, text_lower))

    if work_score > meta_score and work_score > 0:
        return Category.WORK
    if meta_score > work_score and meta_score > 0:
        return Category.META
    if work_score > 0:
        return Category.WORK
    return Category.PERSONAL




def _split_text(text: str) -> list[str]:
    """Split stream-of-consciousness text into individual task fragments."""
    # First, try splitting on explicit list patterns
    # Numbered lists: "1. foo 2. bar 3. baz"
    if re.search(r"\d+[.)]\s", text):
        parts = re.split(r"\d+[.)]\s+", text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            return parts

    # Bullet points
    if re.search(r"^\s*[-*]\s", text, re.MULTILINE):
        parts = re.split(r"\n\s*[-*]\s+", text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            return parts

    # Newline-separated items
    if "\n" in text.strip():
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        if len(lines) > 1 and all(len(l) < 200 for l in lines):
            return lines

    # Now try the heuristic split patterns
    # Build a combined regex that splits on any of our patterns
    fragments = [text]
    for pattern in SPLIT_PATTERNS:
        new_fragments = []
        for frag in fragments:
            parts = re.split(pattern, frag, flags=re.IGNORECASE)
            new_fragments.extend(parts)
        fragments = new_fragments

    # Filter out empty/tiny fragments and merge very short ones back
    result = []
    for frag in fragments:
        frag = frag.strip()
        if len(frag) < 5:
            if result:
                result[-1] = result[-1] + ", " + frag
            continue
        result.append(frag)

    # If we only got one fragment, try comma splitting as last resort
    if len(result) == 1 and "," in text:
        parts = text.split(",")
        # Only use comma splitting if each part seems like a distinct task
        valid_parts = [p.strip() for p in parts if len(p.strip()) > 10]
        if len(valid_parts) > 1:
            return valid_parts

    return result if result else [text]


def parse_brain_dump(text: str) -> list[ParsedTask]:
    """Parse a stream-of-consciousness brain dump into structured tasks.

    Examples:
        "fix the auth bug, review Logan's MR, deploy is blocked on migration"
        -> 3 tasks with appropriate names, categories, and statuses

        "I need to finish the API endpoint and write tests for it,
         also don't forget to update the docs"
        -> 2 tasks
    """
    if not text or not text.strip():
        return []

    fragments = _split_text(text)
    tasks = []

    for fragment in fragments:
        clean = _clean_fragment(fragment)
        if not clean or len(clean) < 3:
            continue

        name = _extract_name(fragment)
        category = _detect_category(fragment)

        tasks.append(ParsedTask(
            raw_text=fragment.strip(),
            name=name,
            category=category,
        ))

    return tasks


def brain_dump_to_workstreams(text: str) -> list[Workstream]:
    """Parse brain dump text and create Workstream objects."""
    tasks = parse_brain_dump(text)
    workstreams = []

    for task in tasks:
        ws = Workstream(
            name=task.name,
            description=task.raw_text,
            category=task.category,
        )
        workstreams.append(ws)

    return workstreams
