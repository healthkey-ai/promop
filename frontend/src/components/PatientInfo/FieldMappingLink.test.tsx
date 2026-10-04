import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import Field from './Field';
import { FieldMappingLinksProvider } from './fieldMappingLinks';

const field = <Field label="Pack years" name="pack_years" type="number" value={3} onChange={vi.fn()} />;

describe('field concept mapping globe', () => {
  it('links an admin to the field in the Field Concept Mapping', () => {
    render(<MemoryRouter><FieldMappingLinksProvider value>{field}</FieldMappingLinksProvider></MemoryRouter>);
    expect(screen.getByRole('link', { name: 'Field concept mapping for Pack years' }))
      .toHaveAttribute('href', '/field-mappings?field=pack_years');
  });

  it('is absent for everyone else, and without a provider', () => {
    const { unmount } = render(<MemoryRouter><FieldMappingLinksProvider value={false}>{field}</FieldMappingLinksProvider></MemoryRouter>);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    unmount();
    render(field);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
