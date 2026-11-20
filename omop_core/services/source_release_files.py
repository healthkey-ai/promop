"""Streaming readers for publisher source terminology (no database or network)."""
import gzip
import io
import re
import zipfile
from defusedxml import ElementTree as ET


def ncit_records(path):
    with zipfile.ZipFile(path) as archive:
        with archive.open('Thesaurus.txt') as raw:
            for number, line in enumerate(io.TextIOWrapper(raw, encoding='utf-8-sig'), 1):
                if not line.strip():
                    continue
                fields = line.rstrip('\r\n').split('\t')
                if len(fields) != 9:
                    raise ValueError(f'Line {number}: expected 9 NCIt fields, got {len(fields)}.')
                code, iri, parents, synonyms, definition, display, status, types, subsets = fields
                names = list(dict.fromkeys(s for s in synonyms.split('|') if s))
                if not re.fullmatch(r'C\d+', code) or not names:
                    raise ValueError(f'Line {number}: missing code or preferred name.')
                yield {
                    'code': code, 'name': names[0], 'definition': definition,
                    'synonyms': names[1:], 'parents': list(filter(None, parents.split('|'))),
                    'semantic_types': list(filter(None, types.split('|'))),
                    'status': status,
                    'retired': bool({'Retired_Concept', 'Obsolete_Concept'} & set(status.split('|'))),
                    'metadata': {'iri': iri, 'display_name': display,
                                 'subsets': list(filter(None, subsets.split('|')))},
                }


def _texts(element, path):
    return list(dict.fromkeys(
        text for child in element.findall(path)
        if (text := ''.join(child.itertext()).strip())
    ))


def _mesh_elements(path, tag):
    # ElementTree does not fetch the publisher's external DTD. Remove each
    # completed record from the root to bound memory even for the full SCR file.
    with gzip.open(path, 'rb') as stream:
        events = ET.iterparse(stream, events=('start', 'end'))
        _, root = next(events)
        for event, element in events:
            if event == 'end' and element.tag == tag:
                yield element
                root.remove(element)


def mesh_records(descriptors, supplementary):
    tree_codes = {}
    for element in _mesh_elements(descriptors, 'DescriptorRecord'):
        code = element.findtext('DescriptorUI')
        for tree in _texts(element, 'TreeNumberList/TreeNumber'):
            tree_codes[tree] = code
    for path, tag, prefix in ((descriptors, 'DescriptorRecord', 'Descriptor'),
                              (supplementary, 'SupplementalRecord', 'SupplementalRecord')):
        for element in _mesh_elements(path, tag):
            code = element.findtext(prefix + 'UI', '').strip()
            name = element.findtext(prefix + 'Name/String', '').strip()
            if not re.fullmatch(r'[DC]\d+', code) or not name:
                raise ValueError('MeSH record missing identifier or preferred name.')
            synonyms = _texts(element, 'ConceptList/Concept/TermList/Term/String')
            # Keep the preferred concept's definition separate from narrower
            # related concepts. SCRs generally describe the substance in Note.
            definitions = _texts(element, "ConceptList/Concept[@PreferredConceptYN='Y']/ScopeNote")
            definitions += [n for n in _texts(element, 'Note') if n not in definitions]
            trees = _texts(element, 'TreeNumberList/TreeNumber')
            parents = sorted({tree_codes[parent] for tree in trees
                              if (parent := tree.rpartition('.')[0]) in tree_codes})
            mapped_headings = [
                {'code': d.findtext('DescriptorUI', '').lstrip('*'),
                 'name': d.findtext('DescriptorName/String', '')}
                for d in element.findall('HeadingMappedToList/HeadingMappedTo/DescriptorReferredTo')
            ]
            actions = [
                {'code': d.findtext('DescriptorUI', ''), 'name': d.findtext('DescriptorName/String', '')}
                for d in element.findall('PharmacologicalActionList/PharmacologicalAction/DescriptorReferredTo')
            ]
            yield {
                'code': code, 'name': name, 'definition': '\n\n'.join(definitions),
                'synonyms': [s for s in synonyms if s != name], 'parents': parents,
                'semantic_types': [], 'status': '', 'retired': False,
                'metadata': {
                    'record_type': 'descriptor' if tag == 'DescriptorRecord' else 'supplementary',
                    'registry_numbers': [n for n in _texts(element, 'ConceptList/Concept/RegistryNumberList/RegistryNumber') if n != '0'],
                    'related_registry_numbers': _texts(element, 'ConceptList/Concept/RelatedRegistryNumberList/RelatedRegistryNumber'),
                    'pharmacological_actions': actions, 'mapped_headings': mapped_headings,
                    'tree_numbers': trees,
                    'annotation': element.findtext('Annotation', '').strip(),
                },
            }
