"""Read-only access to data/facts.yaml. Code never writes this file (plan.md §1, §11)."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config


@dataclass
class FactBank:
    raw: dict
    version: str = "0"
    summaries: dict[str, str] = field(default_factory=dict)
    roles: dict[str, dict] = field(default_factory=dict)          # role id -> role dict
    facts: dict[str, dict] = field(default_factory=dict)          # parent fact id -> {role, variants}
    variants: dict[str, dict] = field(default_factory=dict)       # variant id -> {text, tags, parent, role}
    skills: dict[str, dict] = field(default_factory=dict)         # skill name (lower) -> skill
    education: list[dict] = field(default_factory=list)
    synonyms: set[tuple[str, str]] = field(default_factory=set)   # allowed (from, to), lower-cased

    def text_of(self, ident: str) -> str:
        """Text of any fact id: a variant, a summary, or a parent fact (all variants joined)."""
        if ident in self.variants:
            return self.variants[ident]["text"]
        if ident in self.summaries:
            return self.summaries[ident]
        if ident in self.roles:
            r = self.roles[ident]
            return f"{r.get('title', '')} {r.get('company', '')} {r.get('dates', '')} " + " ".join(
                self.variants[v]["text"] for f in r.get("facts", []) for v in self.facts[f["id"]]["variants"])
        if ident in self.facts:
            return " ".join(self.variants[v]["text"] for v in self.facts[ident]["variants"])
        return ""

    def known_ids(self) -> set[str]:
        return set(self.variants) | set(self.summaries) | set(self.facts)

    def for_prompt(self) -> str:
        """Compact text of the fact bank — identical on every call so it can be prompt-cached."""
        lines = [f"FACT BANK (version {self.version})", "", "Summaries:"]
        lines += [f"- [{i}] {t}" for i, t in self.summaries.items()]
        for rid, role in self.roles.items():
            lines += ["", f"Role [{rid}]: {role.get('title')} at {role.get('company')} ({role.get('dates')})"]
            for f in role.get("facts", []):
                lines.append(f"  Fact [{f['id']}]")
                for v in f.get("variants", []):
                    lines.append(f"    - [{v['id']}] {v['text']}  (tags: {', '.join(v.get('tags', []))})")
        lines += ["", "Skills:"]
        for s in self.skills.values():
            lines.append(f"- {s['name']} ({s.get('level', '')}) evidence: {', '.join(s.get('evidence', []))}")
        if self.synonyms:
            lines += ["", "Allowed substitutions (synonym_map):"]
            lines += [f"- {a} <-> {b}" for a, b in sorted(self.synonyms)]
        return "\n".join(lines)


def parse(raw: dict) -> FactBank:
    fb = FactBank(raw=raw, version=str(raw.get("version", "0")))
    for s in raw.get("summaries", []):
        fb.summaries[s["id"]] = s["text"]
    for role in raw.get("roles", []):
        fb.roles[role["id"]] = role
        for f in role.get("facts", []):
            fb.facts[f["id"]] = {"role": role["id"], "variants": [v["id"] for v in f.get("variants", [])]}
            for v in f.get("variants", []):
                fb.variants[v["id"]] = {"id": v["id"], "text": v["text"], "tags": v.get("tags", []),
                                        "parent": f["id"], "role": role["id"]}
    for s in raw.get("skills", []):
        fb.skills[s["name"].lower()] = s
    fb.education = raw.get("education", [])
    for key, vals in (raw.get("synonym_map") or {}).items():
        for v in vals:
            fb.synonyms.add((key.lower(), v.lower()))
            fb.synonyms.add((v.lower(), key.lower()))
    return fb


def load() -> FactBank:
    return parse(config.load_yaml("facts.yaml"))
