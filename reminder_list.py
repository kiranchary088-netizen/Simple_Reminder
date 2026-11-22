#!/usr/bin/env python3
"""
Simple Reminder List v2.0 (Single-File Edition)
FutureReady Hack – Project #109

Features:
  • Full CRUD (Add, List, Search, Mark Complete, Delete)
  • Automatic status: Overdue / Due today / Upcoming / Completed
  • Pagination, keyword + category + priority search
  • Executive summary with averages & breakdowns
  • Matplotlib charts (status pie + distribution bars)
  • CSV export
  • File locking + atomic JSON writes
  • Structured logging
  • Defensive multi-format date parsing
  • Colored terminal UI

Run:
    python reminder_list.py
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

# Optional but recommended dependencies
try:
    from filelock import FileLock, Timeout
    HAS_FILELOCK = True
except ImportError:
    HAS_FILELOCK = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "reminders.json"
SAMPLE_FILE = BASE_DIR / "sample_reminders.json"
EXPORT_FILE = BASE_DIR / "reminders_export.csv"
LOG_FILE = BASE_DIR / "reminder_app.log"
CHART_DIR = BASE_DIR

CATEGORIES = ["Work", "Personal", "Health", "Finance", "Education", "Other"]
PRIORITIES = {"1": "High", "2": "Medium", "3": "Low"}
PAGE_SIZE = 15

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    logging.getLogger("filelock").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


logger = logging.getLogger("reminder")

# ---------------------------------------------------------------------------
# Terminal cosmetics
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    try:
        return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    except Exception:
        return False


COLOR_ENABLED = _supports_color()


def c(text: str, *codes: int) -> str:
    if not COLOR_ENABLED or not codes:
        return text
    return "\033[" + ";".join(str(x) for x in codes) + "m" + text + "\033[0m"


def bold(t: str) -> str: return c(t, 1)
def dim(t: str) -> str: return c(t, 2)
def red(t: str) -> str: return c(t, 31)
def green(t: str) -> str: return c(t, 32)
def yellow(t: str) -> str: return c(t, 33)
def cyan(t: str) -> str: return c(t, 36)
def white(t: str) -> str: return c(t, 37)


def style_cell(text: str, style_fn: Callable[[str], str], width: int) -> str:
    if not COLOR_ENABLED:
        return f"{text:<{width}}"
    return style_fn(f"{text:<{width}}")


STATUS_STYLES = {
    "Completed": dim,
    "Overdue": lambda t: bold(red(t)),
    "Due today": lambda t: bold(yellow(t)),
    "Upcoming": cyan,
}

STATUS_MARKERS = {
    "Completed": "✅",
    "Overdue": "🔴",
    "Due today": "🟡",
    "Upcoming": "🟢",
}

PRIORITY_STYLES = {
    "High": lambda t: bold(red(t)),
    "Medium": yellow,
    "Low": lambda t: dim(white(t)),
}

CATEGORY_EMOJIS = {
    "Work": "💼", "Personal": "👤", "Health": "💪",
    "Finance": "💰", "Education": "📚", "Other": "📌",
}

BANNER = [
    r"  ____                              _                       ",
    r" |  _ \ _   _  __ _ _ __ ___  _ __(_)_ __   ___ _ __      ",
    r" | | | | | | |/ _` | '_ ` _ \| '__| | '_ \ / _ \ '__|     ",
    r" | |_| | |_| | (_| | | | | | | |  | | | | |  __/ |        ",
    r" |____/ \__,_|\__,_|_| |_| |_|_|  |_|_| |_|\___|_|        ",
    r"   FutureReady Hack · Simple Reminder List · v2.0          ",
]

GOODBYE_QUOTES = [
    "See you soon! 🌟",
    "Back when you need – a reminder awaits! 📝",
    "Catch you later! ⏳",
]


def print_banner(count: Optional[int] = None) -> None:
    print()
    for line in BANNER:
        print(bold(cyan(line)))
    if count is not None:
        print(cyan(f"  Loaded {count} reminder(s). Pick a number to continue."))
    print()


def loading_spinner() -> None:
    label = "  Loading reminders..."
    if not COLOR_ENABLED:
        print(label)
        return
    frames = ["", ".", "..", "...", "....", "...", "..", ".", ""]
    for frame in frames:
        print("\r" + c(label + frame, 36), end="\r", flush=True)
        time.sleep(0.05)
    print("\r" + c(label + " ✔", 36))


def print_goodbye() -> None:
    print()
    print(bold(cyan(f"  {random.choice(GOODBYE_QUOTES)}")))
    print()


# ---------------------------------------------------------------------------
# Domain Model
# ---------------------------------------------------------------------------

class Status(str, Enum):
    COMPLETED = "Completed"
    OVERDUE = "Overdue"
    DUE_TODAY = "Due today"
    UPCOMING = "Upcoming"


@dataclass
class Reminder:
    id: int
    title: str
    due_date: date
    category: str
    priority: str
    is_completed: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

    def __post_init__(self) -> None:
        if not self.title or not str(self.title).strip():
            raise ValueError("Title cannot be empty")
        if self.priority not in ("High", "Medium", "Low"):
            raise ValueError(f"Invalid priority: {self.priority}")

    @property
    def status(self) -> str:
        if self.is_completed:
            return Status.COMPLETED.value
        today = date.today()
        if self.due_date < today:
            return Status.OVERDUE.value
        if self.due_date == today:
            return Status.DUE_TODAY.value
        return Status.UPCOMING.value

    @property
    def days_until(self) -> int:
        if self.is_completed:
            return 0
        return (self.due_date - date.today()).days

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["due_date"] = self.due_date.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Reminder":
        return cls(
            id=int(data["id"]),
            title=str(data["title"]).strip(),
            due_date=date.fromisoformat(str(data["due_date"])),
            category=str(data.get("category", "Other")),
            priority=str(data.get("priority", "Medium")),
            is_completed=bool(data.get("is_completed", False)),
            created_at=str(data.get("created_at", "")),
        )


# ---------------------------------------------------------------------------
# Persistence (with optional file locking + atomic write)
# ---------------------------------------------------------------------------

def _get_lock():
    if not HAS_FILELOCK:
        return None
    lock_path = str(DATA_FILE) + ".lock"
    return FileLock(lock_path, timeout=5.0)


def load_reminders() -> List[Reminder]:
    path = DATA_FILE if DATA_FILE.exists() else (SAMPLE_FILE if SAMPLE_FILE.exists() else None)
    if path is None:
        return []

    lock = _get_lock()
    try:
        if lock:
            lock.acquire()
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return []
        reminders = []
        for item in raw:
            try:
                reminders.append(Reminder.from_dict(item))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping invalid entry: %s", exc)
        return reminders
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load: %s", exc)
        return []
    finally:
        if lock and lock.is_locked:
            lock.release()


def save_reminders(reminders: List[Reminder]) -> None:
    lock = _get_lock()
    try:
        if lock:
            lock.acquire()
        tmp = DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in reminders], f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
        logger.info("Saved %d reminders", len(reminders))
    except OSError as exc:
        logger.exception("Save failed")
        print(red(f"  Could not save reminders: {exc}"))
    finally:
        if lock and lock.is_locked:
            lock.release()


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def parse_date(raw: str) -> Optional[date]:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def get_title() -> str:
    while True:
        title = input("  Enter reminder title: ").strip()
        if title:
            return title
        print(red("  Title cannot be empty."))


def get_due_date() -> date:
    while True:
        raw = input("  Enter due date (YYYY-MM-DD or DD/MM/YYYY): ").strip()
        parsed = parse_date(raw)
        if parsed:
            return parsed
        print(red("  Invalid date. Use YYYY-MM-DD or DD/MM/YYYY."))


def get_category() -> str:
    print(f"  Common categories: {', '.join(CATEGORIES)}")
    while True:
        raw = input("  Category (Enter = Other): ").strip()
        if not raw:
            return "Other"
        if 1 <= len(raw) <= 30:
            return raw
        print(red("  Category must be 1-30 characters."))


def get_priority() -> str:
    print("  Priorities: 1 = High, 2 = Medium, 3 = Low")
    while True:
        raw = input("  Priority (1/2/3, Enter = 2 Medium): ").strip() or "2"
        if raw in PRIORITIES:
            return PRIORITIES[raw]
        print(red("  Please enter 1, 2 or 3."))


def get_yes_no(prompt: str, default: bool = False) -> bool:
    while True:
        raw = input(f"  {prompt} (y/n): ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        if raw == "":
            return default
        print(red("  Please answer y or n."))


def get_int(prompt: str) -> Optional[int]:
    while True:
        raw = input(f"  {prompt}: ").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print(red("  Please enter a valid integer."))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_reminders(
    reminders: List[Reminder],
    header: str,
    total: Optional[int] = None,
    page: int = 1,
) -> None:
    if not reminders and (total is None or total == 0):
        print(f"\n  {header}")
        print("  (no reminders)")
        return

    print(f"\n  {header}")
    print("  " + "-" * 100)
    print(f"  {'ID':>3}  {'Due Date':<11} {'Status':<17} {'Priority':<10} {'Category':<12} Title")
    print("  " + "-" * 100)

    for r in reminders:
        extra = ""
        if r.status == "Due today":
            extra = " (today!)"
        elif r.status == "Overdue":
            extra = f" (overdue by {abs(r.days_until)} day(s))"
        elif r.status == "Upcoming":
            extra = f" (in {r.days_until} day(s))"

        marker = STATUS_MARKERS[r.status]
        status_txt = f"{marker} {r.status}"
        cat_txt = f"{CATEGORY_EMOJIS.get(r.category, '📌')} {r.category}"

        print(
            f"  {r.id:>3}  {r.due_date.isoformat():<11} "
            f"{style_cell(status_txt, STATUS_STYLES[r.status], 17)} "
            f"{style_cell(r.priority, PRIORITY_STYLES.get(r.priority, str), 10)} "
            f"{cat_txt:<12} "
            f"{STATUS_STYLES[r.status](r.title)}{extra}"
        )

    print("  " + "-" * 100)
    total_count = total if total is not None else len(reminders)
    if total_count > PAGE_SIZE:
        start = (page - 1) * PAGE_SIZE + 1
        end = start + len(reminders) - 1
        print(f"  Showing {start}-{end} of {total_count}  |  Page {page}")
    else:
        print(f"  Total: {total_count}")


def print_summary(reminders: List[Reminder]) -> dict:
    total = len(reminders)
    completed = sum(1 for r in reminders if r.is_completed)
    overdue = sum(1 for r in reminders if r.status == "Overdue")
    due_today = sum(1 for r in reminders if r.status == "Due today")
    upcoming = sum(1 for r in reminders if r.status == "Upcoming")
    active = [r for r in reminders if not r.is_completed]
    avg_days = round(sum(r.days_until for r in active) / len(active), 1) if active else 0.0

    print()
    print(bold(cyan("  --- Executive Summary ---")))
    print(f"  Total reminders   : {bold(str(total))}")
    print(f"  Completed         : {green(str(completed))}")
    print(f"  Overdue           : {red(str(overdue))}")
    print(f"  Due today         : {yellow(str(due_today))}")
    print(f"  Upcoming          : {cyan(str(upcoming))}")
    print(f"  Avg days to due   : {avg_days} (non-completed only)")

    if overdue:
        print(bold(red(f"  ⚠ WARNING: {overdue} reminder(s) are overdue.")))

    if active:
        earliest = min(active, key=lambda r: r.due_date)
        print(f"  Earliest due      : {earliest.due_date.isoformat()} "
              f"(#{earliest.id} – {earliest.title})")

    by_cat = dict(Counter(r.category for r in reminders).most_common())
    by_pri = dict(Counter(r.priority for r in reminders).most_common())

    print(bold("  By category:"))
    for cat, n in by_cat.items():
        print(f"    {CATEGORY_EMOJIS.get(cat, '📌')} {cat:<12} : {n}")

    print(bold("  By priority:"))
    for pri in ("High", "Medium", "Low"):
        n = by_pri.get(pri, 0)
        style = PRIORITY_STYLES.get(pri, str)
        print(f"    {style(f'{pri:<8} : {n}')}")

    return {
        "total": total, "completed": completed, "overdue": overdue,
        "due_today": due_today, "upcoming": upcoming,
        "by_category": by_cat, "by_priority": by_pri,
    }


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def generate_charts(stats: dict) -> List[Path]:
    if not HAS_MATPLOTLIB:
        print(yellow("  Matplotlib not installed – skipping charts."))
        print(yellow("  Install with: pip install matplotlib"))
        return []

    saved: List[Path] = []

    # Status pie
    labels = ["Completed", "Overdue", "Due today", "Upcoming"]
    values = [stats["completed"], stats["overdue"], stats["due_today"], stats["upcoming"]]
    colors = ["#4CAF50", "#F44336", "#FFC107", "#2196F3"]
    non_zero = [(l, v, c) for l, v, c in zip(labels, values, colors) if v > 0]

    if non_zero:
        labs, vals, cols = zip(*non_zero)
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.pie(vals, labels=labs, colors=cols, autopct="%1.1f%%", startangle=90)
        ax.set_title(f"Reminder Status (Total: {stats['total']})", fontweight="bold")
        path = CHART_DIR / "reminder_status_pie.png"
        fig.tight_layout()
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        saved.append(path)

    # Category + Priority bars
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    if stats["by_category"]:
        cats = list(stats["by_category"].keys())
        counts = list(stats["by_category"].values())
        ax1.barh(cats, counts, color="#3F51B5")
        ax1.set_xlabel("Count")
        ax1.set_title("By Category")
        ax1.invert_yaxis()

    pri_order = ["High", "Medium", "Low"]
    pri_counts = [stats["by_priority"].get(p, 0) for p in pri_order]
    ax2.bar(pri_order, pri_counts, color=["#F44336", "#FFC107", "#9E9E9E"])
    ax2.set_ylabel("Count")
    ax2.set_title("By Priority")

    fig.suptitle("Reminder Distribution", fontweight="bold")
    fig.tight_layout()
    path2 = CHART_DIR / "reminder_distribution.png"
    fig.savefig(path2, dpi=120, bbox_inches="tight")
    plt.close(fig)
    saved.append(path2)

    return saved


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def add_reminder(reminders: List[Reminder]) -> None:
    print("\n  --- Add Reminder ---")
    title = get_title()
    due = get_due_date()
    category = get_category()
    priority = get_priority()
    new_id = max((r.id for r in reminders), default=0) + 1
    r = Reminder(new_id, title, due, category, priority)
    reminders.append(r)
    save_reminders(reminders)
    print(bold(cyan(f"  ✔ Added #{r.id}: ")) + bold(r.title) +
          cyan(f"  due {r.due_date.isoformat()} ({r.category}, {r.priority})"))


def list_with_pagination(reminders: List[Reminder], header: str) -> None:
    items = sorted(reminders, key=lambda r: (r.due_date, r.id))
    total = len(items)
    page = 1
    while True:
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        print_reminders(items[start:end], header, total=total, page=page)
        if total <= PAGE_SIZE:
            break
        nav = input("  [n]ext / [p]rev / [q]uit: ").strip().lower()
        if nav == "n" and end < total:
            page += 1
        elif nav == "p" and page > 1:
            page -= 1
        else:
            break


def search_reminders(reminders: List[Reminder]) -> None:
    keyword = input("  Search keyword (title or category, blank = all): ").strip().lower()
    category = input("  Category filter (blank = all): ").strip().lower()
    priority = input("  Priority filter (High/Medium/Low, blank = all): ").strip().lower()

    results = [
        r for r in reminders
        if (not keyword or keyword in r.title.lower() or keyword in r.category.lower())
        and (not category or r.category.lower() == category)
        and (not priority or r.priority.lower() == priority)
    ]
    results = sorted(results, key=lambda r: (r.due_date, r.id))
    print_reminders(results, f"Search Results ({len(results)} matched)")


def toggle_complete(reminders: List[Reminder]) -> None:
    rid = get_int("Enter ID of reminder to toggle completion")
    if rid is None:
        return
    target = next((r for r in reminders if r.id == rid), None)
    if target is None:
        print(red(f"  No reminder with ID #{rid}."))
        return
    new_state = not target.is_completed
    action = "complete" if new_state else "incomplete"
    if get_yes_no(f"Mark #{rid} '{target.title}' as {action}?", default=True):
        target.is_completed = new_state
        save_reminders(reminders)
        print(green(f"  ✔ #{rid} is now {'completed' if new_state else 'incomplete'}."))


def delete_reminder(reminders: List[Reminder]) -> None:
    rid = get_int("Enter ID of reminder to delete")
    if rid is None:
        return
    target = next((r for r in reminders if r.id == rid), None)
    if target is None:
        print(red(f"  No reminder with ID #{rid}."))
        return
    if get_yes_no(f"Delete #{rid} '{target.title}'?", default=False):
        reminders.remove(target)
        save_reminders(reminders)
        print(green(f"  ✔ Deleted #{rid}."))
    else:
        print("  Not deleted.")


def export_csv(reminders: List[Reminder]) -> None:
    items = sorted(reminders, key=lambda r: (r.due_date, r.id))
    try:
        with open(EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "title", "due_date", "category", "priority",
                "status", "is_completed", "created_at", "days_until"
            ])
            for r in items:
                writer.writerow([
                    r.id, r.title, r.due_date.isoformat(), r.category,
                    r.priority, r.status, r.is_completed, r.created_at, r.days_until
                ])
        print(green(f"  ✔ Exported {len(items)} reminder(s) to {EXPORT_FILE}"))
    except OSError as exc:
        print(red(f"  Could not export: {exc}"))


def view_summary(reminders: List[Reminder]) -> None:
    stats = print_summary(reminders)
    if get_yes_no("Generate Matplotlib charts?", default=True):
        paths = generate_charts(stats)
        if paths:
            print(green("  ✔ Charts saved:"))
            for p in paths:
                print(f"    → {p}")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    reminders = load_reminders()
    print_banner(len(reminders))
    loading_spinner()

    while True:
        print("\n  --- Main Menu ---")
        print("  1. Add Reminder")
        print("  2. List All Reminders")
        print("  3. List Active / Upcoming")
        print("  4. Search Reminders")
        print("  5. View Summary + Charts")
        print("  6. Mark Complete / Incomplete")
        print("  7. Delete Reminder")
        print("  8. Export to CSV")
        print("  9. Exit  (or 'q')")
        choice = input("  Select option (1-9): ").strip().lower()

        try:
            if choice == "1":
                add_reminder(reminders)
            elif choice == "2":
                list_with_pagination(reminders, "All Reminders")
            elif choice == "3":
                active = [r for r in reminders if r.status in ("Overdue", "Due today", "Upcoming")]
                list_with_pagination(active, "Active Reminders (Overdue + Due today + Upcoming)")
            elif choice == "4":
                search_reminders(reminders)
            elif choice == "5":
                view_summary(reminders)
            elif choice == "6":
                toggle_complete(reminders)
            elif choice == "7":
                delete_reminder(reminders)
            elif choice == "8":
                export_csv(reminders)
            elif choice in ("9", "q", "exit"):
                print_goodbye()
                break
            else:
                print(red("  Invalid choice. Please pick 1-9."))
        except KeyboardInterrupt:
            print("\n  Interrupted.")
            break
        except Exception as exc:
            logger.exception("Unhandled error")
            print(red(f"  Unexpected error: {exc}"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted. Bye!")
