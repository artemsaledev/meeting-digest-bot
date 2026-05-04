from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class Person:
    full_name: str
    bitrix_user_id: int
    profile_url: str
    aliases: tuple[str, ...]
    telegram_username: str = ""


class PeopleDirectory:
    def __init__(self, people: list[Person]) -> None:
        self.people = people
        self._alias_index: dict[str, Person] = {}
        for person in people:
            for alias in (person.full_name, *person.aliases):
                normalized = self.normalize_name(alias)
                if normalized:
                    self._alias_index[normalized] = person

    @classmethod
    def from_file(cls, path: Path | None = None) -> "PeopleDirectory":
        source = path or Path(__file__).with_name("people_directory.json")
        raw_people = json.loads(source.read_text(encoding="utf-8"))
        people = [
            Person(
                full_name=str(item["full_name"]),
                bitrix_user_id=int(item["bitrix_user_id"]),
                profile_url=str(item["profile_url"]),
                aliases=tuple(str(alias) for alias in item.get("aliases", [])),
                telegram_username=str(item.get("telegram_username") or "").strip(),
            )
            for item in raw_people
        ]
        return cls(people)

    def find(self, name: str) -> Person | None:
        normalized = self.normalize_name(name)
        if not normalized:
            return None
        exact = self._alias_index.get(normalized)
        if exact:
            return exact
        candidates = sorted(self._alias_index.items(), key=lambda item: len(item[0]), reverse=True)
        for alias, person in candidates:
            if self._name_contains_alias(normalized, alias):
                return person
        return None

    def bitrix_user_id_for(self, name: str) -> int | None:
        person = self.find(name)
        return person.bitrix_user_id if person else None

    def find_by_bitrix_user_id(self, bitrix_user_id: int | str) -> Person | None:
        try:
            target_id = int(bitrix_user_id)
        except (TypeError, ValueError):
            return None
        for person in self.people:
            if person.bitrix_user_id == target_id:
                return person
        return None

    @staticmethod
    def normalize_name(value: str) -> str:
        text = value.replace("ё", "е").replace("Ё", "Е").casefold()
        text = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _name_contains_alias(normalized_name: str, normalized_alias: str) -> bool:
        if not normalized_alias:
            return False
        return (
            normalized_name == normalized_alias
            or normalized_name.startswith(normalized_alias + " ")
            or normalized_name.endswith(" " + normalized_alias)
            or f" {normalized_alias} " in f" {normalized_name} "
        )
