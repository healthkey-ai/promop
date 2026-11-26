"""Stable, scoped allowed values for a PatientRecord field (#1224).

A ``FieldChoice`` used to be known by its display text, which a curator can
edit, and offered to every patient whatever their disease. This module holds
the rules that make it an identified, scoped value:

* **identity** is ``(field_name, context_key, code)``; the code is made once
  from the label and never changes with it (``next_code``);
* **aliases** are other spellings that name the same choice (an earlier
  label, a source's wording); a spelling names one choice within a field and
  context, which the database holds (``FieldChoiceAlias``) and ``set_aliases``
  reports in words;
* **scope** is the diseases that offer a choice (``set_diseases``), none
  meaning all of them, and ``resolve`` / ``offered`` read it, so a record is
  never handed another disease's answers by accident;
* **value** is typed (``clean_value``), so a number is not stored as text.

Writers use these functions rather than the models, so every path applies the
same checks. Nothing here touches a stored patient value.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from omop_core.services import disease_profiles

CODE_MAX = 100
ALIAS_MAX = 200
_NON_SLUG = re.compile(r'[^a-z0-9]+')
_BOOLEAN = {'true': 'true', 'false': 'false'}


class ChoiceError(ValueError):
    """A choice, alias, scope or value the rules refuse; the text is for a curator."""


class AmbiguousChoice(ChoiceError):
    """A spelling that more than one choice of the field answers to."""


def fold(text: str) -> str:
    """A spelling folded for comparison: case and runs of whitespace do not matter."""
    return ' '.join(str(text or '').split()).casefold()


def slug(text: str) -> str:
    return _NON_SLUG.sub('_', str(text or '').casefold()).strip('_')


def next_code(choice) -> str:
    """A code for ``choice`` that no other choice of its field and context holds.

    Made from the label, so a seeded row reads ``stage_iv`` rather than a
    number, and suffixed ``_2``, ``_3`` when two labels slug alike. Called once,
    when the row is first saved; a later rename never reaches it.
    """
    from omop_core.models import FieldChoice

    base = (slug(choice.display) or 'value')[:CODE_MAX]
    taken = set(
        FieldChoice.objects
        .filter(field_name=choice.field_name, context_key=choice.context_key)
        .exclude(pk=choice.pk).values_list('code', flat=True)
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f'_{n}'
        candidate = f'{base[:CODE_MAX - len(suffix)]}{suffix}'
        if candidate not in taken:
            return candidate
        n += 1


def clean_value(value_type: str, value: str) -> str:
    """``value`` as the canonical text of its type; blank stays blank (the display)."""
    text = str(value or '').strip()
    if not text:
        return ''
    if value_type == 'text':
        return text
    if value_type == 'integer':
        try:
            return str(int(text))
        except ValueError:
            raise ChoiceError(f'{text!r} is not a whole number.') from None
    if value_type == 'decimal':
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ChoiceError(f'{text!r} is not a number.') from None
        if not number.is_finite():
            raise ChoiceError(f'{text!r} is not a number.')
        return format(number.normalize(), 'f')
    if value_type == 'boolean':
        folded = text.casefold()
        if folded not in _BOOLEAN:
            raise ChoiceError(f'{text!r} is not true or false.')
        return _BOOLEAN[folded]
    raise ChoiceError(f'Unknown value type {value_type!r}.')


def validate(choice) -> None:
    """Refuse a choice the rules cannot hold, before it is saved."""
    if not str(choice.display or '').strip():
        raise ChoiceError('A choice needs a label.')
    if not str(choice.code or '').strip() and not choice.pk:
        return  # the code is made on save
    if choice.code and slug(choice.code) != choice.code:
        raise ChoiceError(
            f'The code {choice.code!r} must be lower case letters, digits and underscores.')
    choice.value = clean_value(choice.value_type, choice.value)


def _taken_spellings(field_name: str, context_key: str, *, exclude_choice=None) -> dict[str, int]:
    """Every folded spelling the field and context already answers to, with its choice."""
    from omop_core.models import FieldChoice, FieldChoiceAlias

    spellings: dict[str, int] = {}
    choices = FieldChoice.objects.filter(field_name=field_name, context_key=context_key)
    for pk, display, code in choices.values_list('pk', 'display', 'code'):
        spellings[fold(display)] = pk
        spellings.setdefault(fold(code), pk)
    for pk, key in (FieldChoiceAlias.objects
                    .filter(field_name=field_name, context_key=context_key)
                    .values_list('choice_id', 'alias_key')):
        spellings.setdefault(key, pk)
    if exclude_choice is not None:
        spellings = {k: v for k, v in spellings.items() if v != exclude_choice.pk}
    return spellings


def set_aliases(choice, aliases: Iterable[str], *, source: str = 'curator') -> list[str]:
    """Make ``aliases`` the other spellings of ``choice``; the label is not one.

    A spelling another choice of the field and context already answers to,
    as its label, code or alias, is refused with the choice's name: resolving
    it would have to guess. The choice's own earlier labels (``source='label'``)
    are kept whatever ``aliases`` holds, since stored data may still read them.
    """
    from omop_core.models import FieldChoiceAlias

    wanted: dict[str, str] = {}
    for alias in aliases:
        text = ' '.join(str(alias or '').split())
        if not text:
            continue
        if len(text) > ALIAS_MAX:
            raise ChoiceError(f'An alias may be at most {ALIAS_MAX} characters.')
        wanted.setdefault(fold(text), text)
    with transaction.atomic():
        taken = _taken_spellings(choice.field_name, choice.context_key, exclude_choice=choice)
        for key, text in wanted.items():
            if key in taken:
                owner = type(choice).objects.filter(pk=taken[key]).values_list('display', flat=True).first()
                raise ChoiceError(
                    f'{text!r} already means {owner!r} for {choice.field_name} '
                    f'in this context, so it cannot also mean {choice.display!r}.')
        own = FieldChoiceAlias.objects.filter(choice=choice)
        keep_labels = set(own.filter(source='label').values_list('alias_key', flat=True))
        own.exclude(alias_key__in=keep_labels).exclude(alias_key__in=wanted).delete()
        have = set(own.values_list('alias_key', flat=True))
        FieldChoiceAlias.objects.bulk_create([
            FieldChoiceAlias(choice=choice, field_name=choice.field_name,
                             context_key=choice.context_key, alias=text, alias_key=key,
                             source=source)
            for key, text in wanted.items()
            if key not in have and key != fold(choice.display)
        ])
        return sorted(choice.aliases.values_list('alias', flat=True))


def rename(choice, display: str) -> None:
    """Change the label and keep the old one as an alias, so nothing stored is orphaned.

    What a save writes for a choice is its stored value, which is the label
    until a value is set. A rename must not change that, or saves made before
    and after it would disagree, so the label being replaced is frozen into
    ``value`` first when the choice has none (#1225). It is done here, at the
    first rename, so a choice nobody renames stays as it was seeded.
    """
    from omop_core.models import FieldChoiceAlias

    new = ' '.join(str(display or '').split())
    if not new:
        raise ChoiceError('A choice needs a label.')
    if new == choice.display:
        return
    with transaction.atomic():
        taken = _taken_spellings(choice.field_name, choice.context_key, exclude_choice=choice)
        if fold(new) in taken:
            raise ChoiceError(f'{new!r} already means another choice of {choice.field_name} in this context.')
        old = choice.display
        FieldChoiceAlias.objects.filter(choice=choice, alias_key=fold(new)).delete()
        choice.display = new
        fields = ['display']
        if not choice.value:
            choice.value = old
            fields.append('value')
        choice.save(update_fields=fields)
        if fold(old) != fold(new):
            FieldChoiceAlias.objects.get_or_create(
                field_name=choice.field_name, context_key=choice.context_key, alias_key=fold(old),
                defaults={'choice': choice, 'alias': old, 'source': 'label'},
            )


def set_diseases(choice, codes: Iterable[str]) -> list[str]:
    """Make ``codes`` the diseases that offer ``choice``; none offers it to all."""
    from omop_core.models import FieldChoiceDisease

    try:
        wanted = disease_profiles.clean_codes(codes)
    except disease_profiles.ScopeError as exc:
        raise ChoiceError(str(exc)) from None
    with transaction.atomic():
        rows = FieldChoiceDisease.objects.filter(choice=choice)
        have = set(rows.values_list('disease', flat=True))
        rows.exclude(disease__in=wanted).delete()
        FieldChoiceDisease.objects.bulk_create([
            FieldChoiceDisease(choice=choice, disease=code) for code in wanted if code not in have
        ])
    return wanted


def set_retired(choice, retired: bool) -> None:
    """Stop offering a choice, or offer it again. Stored values are not touched."""
    if retired == (choice.retired_at is not None):
        return
    choice.retired_at = timezone.now() if retired else None
    choice.save(update_fields=['retired_at'])


def _in_scope(choice, disease: str | None) -> bool:
    diseases = {row.disease for row in choice.disease_scope.all()}
    return not diseases or (disease is not None and disease in diseases)


def offered(field_name: str, disease: str | None = None, *, context_key: str | None = None) -> list:
    """The choices a record with Disease Category ``disease`` is offered, in order.

    Retired choices are left out; one with no diseases is offered to everyone.
    ``disease=None`` is a record with no category, which is offered only the
    unscoped choices.
    """
    from omop_core.models import FieldChoice

    qs = (FieldChoice.objects.filter(field_name=field_name, retired_at__isnull=True)
          .exclude(context_key__startswith='lookup:')  # decisions about lookup entries, not answers
          .prefetch_related('disease_scope'))
    if context_key is not None:
        qs = qs.filter(context_key=context_key)
    return [choice for choice in qs if _in_scope(choice, disease)]


def resolve(field_name: str, text: str, disease: str | None = None, *, include_retired: bool = True):
    """The one choice ``text`` names for a record of ``disease``, or ``None``.

    ``text`` may be a code, the label, the stored value or an alias, compared
    folded. A spelling several choices answer to in scope, ISS's "I" and Ann
    Arbor's "I" for a patient the diseases of both reach, raises
    ``AmbiguousChoice`` rather than picking one; the disease is what normally
    separates them. A scoped choice the disease is not in is not a match.
    """
    from omop_core.models import FieldChoice

    key = fold(text)
    if not key:
        return None
    qs = (FieldChoice.objects.filter(field_name=field_name)
          .prefetch_related('disease_scope', 'aliases'))
    if not include_retired:
        qs = qs.filter(retired_at__isnull=True)
    found = []
    for choice in qs:
        spellings = {fold(choice.display), fold(choice.code), fold(choice.stored_value)}
        spellings.update(alias.alias_key for alias in choice.aliases.all())
        if key in spellings and _in_scope(choice, disease):
            found.append(choice)
    if len(found) > 1:
        raise AmbiguousChoice(
            f'{text!r} names more than one choice of {field_name}: '
            + ', '.join(sorted(f'{c.display} ({c.context_key or "any context"})' for c in found)) + '.')
    return found[0] if found else None
