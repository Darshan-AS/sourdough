"""Human-friendly name and ID resolution for Accounts, Categories, Currencies, and Labels."""

from collections.abc import Callable, Iterable

from sourdough.domain.models import (
    AccountDocument,
    CategoryDocument,
    CurrencyDocument,
    LabelDocument,
)
from sourdough.exceptions import AmbiguousEntityError, EntityNotFoundError
from sourdough.infra.cache import MetadataCache, MetadataSnapshot


class EntityResolver:
    """Resolves human names, UUIDs, or envelope IDs to validated domain entities."""

    def __init__(self, cache: MetadataCache) -> None:
        self.cache = cache

    def _with_fallback_refresh[T](self, lookup_fn: Callable[[MetadataSnapshot], T]) -> T:
        snapshot = self.cache.get_snapshot(force_refresh=False)
        try:
            return lookup_fn(snapshot)
        except EntityNotFoundError:
            fresh = self.cache.get_snapshot(force_refresh=True)
            return lookup_fn(fresh)

    def resolve_account(self, query: str) -> AccountDocument:
        return self._with_fallback_refresh(lambda snap: self._find_account(snap, query))

    def resolve_category(self, query: str) -> CategoryDocument:
        return self._with_fallback_refresh(lambda snap: self._find_category(snap, query))

    def resolve_transfer_category(self) -> CategoryDocument:
        return self._with_fallback_refresh(self._find_transfer_category)

    def resolve_currency(self, query: str) -> CurrencyDocument:
        return self._with_fallback_refresh(lambda snap: self._find_currency(snap, query))

    def resolve_label(self, query: str) -> LabelDocument:
        return self._with_fallback_refresh(lambda snap: self._find_label(snap, query))

    def resolve_labels(self, queries: Iterable[str]) -> tuple[str, ...]:
        q_list = [q.strip() for q in queries if q and q.strip()]
        if not q_list:
            return ()
        resolved: list[str] = []
        for q in q_list:
            lbl = self.resolve_label(q)
            if lbl.doc_id not in resolved:
                resolved.append(lbl.doc_id)
        return tuple(resolved)

    @staticmethod
    def _find_account(snap: MetadataSnapshot, query: str) -> AccountDocument:
        q = query.strip()
        if not q:
            raise EntityNotFoundError("Account query cannot be empty")
        for acc in snap.accounts:
            if acc.doc_id == q or acc.doc_id == f"-Account_{q}":
                return acc

        exact_ci = [a for a in snap.accounts if a.name.lower() == q.lower()]
        if len(exact_ci) == 1:
            return exact_ci[0]
        if len(exact_ci) > 1:
            options = ", ".join(f"{a.name!r} ({a.doc_id})" for a in exact_ci)
            raise AmbiguousEntityError(f"Multiple accounts match {query!r}: {options}")

        partial = [a for a in snap.accounts if q.lower() in a.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            options = ", ".join(f"{a.name!r} ({a.doc_id})" for a in partial)
            raise AmbiguousEntityError(
                f"Ambiguous account query {query!r}; matches: {options}"
            )
        available = ", ".join(sorted(a.name for a in snap.accounts))
        raise EntityNotFoundError(
            f"Account {query!r} not found. Available accounts: {available}"
        )

    @staticmethod
    def _find_category(snap: MetadataSnapshot, query: str) -> CategoryDocument:
        q = query.strip()
        if not q:
            raise EntityNotFoundError("Category query cannot be empty")

        for cat in snap.categories:
            if cat.doc_id == q or cat.doc_id == f"-Category_{q}":
                return cat

        if ">" in q:
            parent_q, child_q = [p.strip() for p in q.split(">", 1)]
            if not parent_q or not child_q:
                raise EntityNotFoundError(f"Invalid hierarchical category query {query!r}")
            parent_env_ids: set[int] = set()
            if parent_q.isdigit():
                parent_env_ids.add(int(parent_q))
            else:
                for c in snap.categories:
                    if (
                        c.display_name.lower() == parent_q.lower()
                        or c.name.lower() == parent_q.lower()
                        or parent_q.lower() in c.display_name.lower()
                    ):
                        parent_env_ids.add(c.envelope_id)
            scoped = [c for c in snap.categories if c.envelope_id in parent_env_ids]
            exact_child = [
                c
                for c in scoped
                if c.display_name.lower() == child_q.lower()
                or c.name.lower() == child_q.lower()
            ]
            if len(exact_child) == 1:
                return exact_child[0]
            if len(exact_child) > 1:
                custom_child = [c for c in exact_child if c.custom_category]
                if len(custom_child) == 1:
                    return custom_child[0]
            partial_child = [
                c
                for c in scoped
                if child_q.lower() in c.display_name.lower()
                or child_q.lower() in c.name.lower()
            ]
            if len(partial_child) == 1:
                return partial_child[0]
            if len(partial_child) > 1:
                options = ", ".join(
                    f"{c.display_name!r} ({c.doc_id}, envelope={c.envelope_id})"
                    for c in partial_child
                )
                raise AmbiguousEntityError(
                    f"Multiple categories match {query!r}: {options}"
                )
            raise EntityNotFoundError(
                f"Subcategory {child_q!r} under {parent_q!r} not found."
            )

        if q.isdigit():
            env_id = int(q)
            env_matches = [
                c for c in snap.categories if c.envelope_id == env_id and not c.custom_category
            ]
            if len(env_matches) == 1:
                return env_matches[0]
            env_all = [c for c in snap.categories if c.envelope_id == env_id]
            if len(env_all) == 1:
                return env_all[0]

        exact_ci = [
            c
            for c in snap.categories
            if c.display_name.lower() == q.lower() or c.name.lower() == q.lower()
        ]
        if len(exact_ci) == 1:
            return exact_ci[0]
        if len(exact_ci) > 1:
            built_in = [c for c in exact_ci if not c.custom_category]
            if len(built_in) == 1:
                return built_in[0]
            options = ", ".join(
                f"{c.display_name!r} ({c.doc_id}, envelope={c.envelope_id})" for c in exact_ci
            )
            raise AmbiguousEntityError(f"Multiple categories match {query!r}: {options}")

        partial = [
            c
            for c in snap.categories
            if q.lower() in c.display_name.lower() or q.lower() in c.name.lower()
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            options = ", ".join(
                f"{c.display_name!r} ({c.doc_id})" for c in partial[:8]
            )
            raise AmbiguousEntityError(
                f"Ambiguous category query {query!r}; matches: {options}"
            )
        raise EntityNotFoundError(
            f"Category {query!r} not found. Run 'dough categories --search ...' to list categories."
        )

    @staticmethod
    def _find_transfer_category(snap: MetadataSnapshot) -> CategoryDocument:
        for cat in snap.categories:
            if cat.is_transfer_category:
                return cat
        for cat in snap.categories:
            if "transfer" in cat.display_name.lower():
                return cat
        raise EntityNotFoundError("System Transfer category (envelopeId=20001) not found in CouchDB")

    @staticmethod
    def _find_currency(snap: MetadataSnapshot, query: str) -> CurrencyDocument:
        q = query.strip()
        for cur in snap.currencies:
            if (
                cur.doc_id == q
                or cur.doc_id == f"-Currency_{q}"
                or cur.code.lower() == q.lower()
            ):
                return cur
        available = ", ".join(sorted(c.code for c in snap.currencies))
        raise EntityNotFoundError(
            f"Currency {query!r} not found. Available currencies: {available}"
        )

    @staticmethod
    def _find_label(snap: MetadataSnapshot, query: str) -> LabelDocument:
        q = query.strip()
        if q.startswith("#") and len(q) > 1:
            q = q[1:].strip()
        for lbl in snap.labels:
            if lbl.doc_id == q or lbl.doc_id == f"-HashTag_{q}" or lbl.name.lower() == q.lower():
                return lbl
        partial = [lbl for lbl in snap.labels if q.lower() in lbl.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            options = ", ".join(f"{l.name!r} ({l.doc_id})" for l in partial)
            raise AmbiguousEntityError(f"Multiple labels match {query!r}: {options}")
        raise EntityNotFoundError(f"Label {query!r} not found")
