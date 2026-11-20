"""Display context taken only from a source FHIR resource."""

def based_on_labels(metadata):
    """Labels from the source FHIR resource's basedOn references only."""
    if not isinstance(metadata, dict):
        return []
    resource = metadata.get('fhir', metadata)
    if not isinstance(resource, dict):
        return []
    references = resource.get('basedOn', [])
    if not isinstance(references, list):
        return []
    labels = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        for key in ('display', 'reference'):
            value = reference.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
                break
    return labels
