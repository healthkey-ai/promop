"""The grouping key behind one curated decision: the normalised source label.

A vendor-local code is opaque -- an Epic flowsheet id, a Cerner ``codeSet/72``
number -- and carries no meaning a curator or a ranker can use. Its *display
text* does, and the same text arrives under thousands of different codes: in a
721,620-code HealthTree extract, ``albumin`` appeared under 2,557 codes,
``glucose`` under 2,032, ``creatinine`` under 1,646. Collapsing on the text
turns 721,620 rows into 189,276, and the top 500 of those cover 51% of
19M records.

The row key stays ``(source_vocabulary_id, source_code)``: ingest resolves on a
code (``mapping.code_resolution.resolve_source_code``) and never on a label, so
nothing here changes what a lookup finds. This is a *grouping* key, for showing
one review row per label and asking the ranker once per label instead of once
per code.

The rule, chosen by measuring each part against 174,821 real display strings:

* **lowercase** -- vendors disagree on case for the same analyte.
* **keep ``%`` and ``#``** -- dropping them merges 1,027 groups, and they are
  the top analytes in the corpus. ``neutrophils``, ``neutrophils %`` and
  ``neutrophils #`` are three different LOINC concepts: a relative percentage
  and an absolute count are not the same measurement.
* **keep parenthesised text** -- stripping ``(...)`` merges 4,343 groups and
  loses real distinctions: ``RDW (CV)`` and ``RDW (SD)`` are different LOINCs
  but both become ``rdw``; ``Comment (bed mobility)`` becomes ``comment``.
* **remove every other non-alphanumeric character**, rather than replacing it
  with a space. This merges 1,334 groups and every one of the 25 largest is the
  same analyte spelled differently -- ``gamma globulin``/``gammaglobulin``,
  ``e gfr``/``egfr``, ``a g ratio``/``ag ratio``/``agratio``,
  ``m spike``/``mspike``, ``abo rh``/``aborh``. Substituting a space keeps all
  of those apart, splitting one analyte across several curator decisions.

The key is therefore not human-readable (``gammaglobulin``, ``nrbc%``). It is
never displayed: a group shows its members' ``source_code_description``.

**Empty normalises to NULL, and NULL means "not part of any group."** 1,925
queue rows on staging carry no description at all, and a label of ``--`` or
whitespace normalises to nothing. Grouping those together would offer a curator
a single row standing for 1,929 unrelated codes, mappable in one click. A
``GROUP BY`` still groups NULLs, so the rollup must exclude them explicitly --
``.exclude(source_label_norm=None)`` -- and this module returning ``None``
rather than ``''`` is what makes that exclusion expressible.

**A description at its storage limit forms no group either.** ``source_code_description``
is ``varchar(255)``, so a longer label is stored truncated, and two different
labels sharing a 255-character prefix become one key. On staging that merges 28
groups covering 76 codes -- distinct CPT procedures whose prose differs only
past the cut, offered as a single row a curator could map in one click. A label
at the limit cannot be told from a truncated one, so it is not trusted as an
identity. Real analyte labels are short; 255 characters of text is procedure
prose, which this grouping is not for.

**Widening source_code_description needs three steps.** Postgres refuses
``ALTER COLUMN ... TYPE`` on a column a stored generated column reads --
``cannot alter type of a column used by a generated column`` -- so a migration
that only widens the description fails. Drop this column, alter the
description, then re-add it, in one migration:

.. code-block:: python

    operations = [
        migrations.RemoveField('sourcecodeconceptmapping', 'source_label_norm'),
        migrations.AlterField('sourcecodeconceptmapping', 'source_code_description', ...),
        migrations.AddField('sourcecodeconceptmapping', 'source_label_norm', ...),
    ]

Re-adding recomputes every row, so nothing is lost. Also lower
:data:`MAX_DESCRIPTION` in step with the new length, or labels that are no
longer truncated keep being refused a key.

**This rule is frozen.** The normalised value is the identity a review group is
built from; changing it re-keys every group. It is stored in a database
generated column so the Python here and the SQL there cannot drift, which also
means changing it is a migration, deliberately.
"""
import re

# Characters that survive normalisation. Kept in one place because the SQL
# generated column below must use the same set.
_KEEP = 'a-z0-9%#'
_STRIP = re.compile(f'[^{_KEEP}]+')

#: The SQL the database generated column evaluates. Mirrors :func:`normalise`.
SQL_PATTERN = f'[^{_KEEP}]+'

#: ``SourceCodeConceptMapping.source_code_description``'s ``max_length``. A
#: label this long may have been cut to fit, so it is not a reliable identity.
#: It cannot be imported from the model -- the model imports this module -- so
#: ``test_max_description_matches_the_model_field`` asserts the two agree.
MAX_DESCRIPTION = 255


def normalise(label):
    """Return the grouping key for ``label``, or ``None`` when there is none.

    ``None`` is returned for an empty, blank or punctuation-only label -- see
    the module docstring on why that is not the empty string.

    >>> normalise('Gamma Globulin')
    'gammaglobulin'
    >>> normalise('NRBC %')
    'nrbc%'
    >>> normalise('RDW (CV)')
    'rdwcv'
    >>> normalise('--') is None
    True
    >>> normalise('x' * 255) is None
    True
    """
    label = label or ''
    if len(label) >= MAX_DESCRIPTION:
        return None
    return _STRIP.sub('', label.lower()) or None
