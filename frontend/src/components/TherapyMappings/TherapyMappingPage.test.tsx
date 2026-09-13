import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, expect, it, vi } from 'vitest';
import * as catalog from '@/api/mappingHub';
import TherapyMappingPage from './TherapyMappingPage';

vi.mock('@/api/mappingHub');

beforeEach(() => {
  vi.mocked(catalog.fetchRegimens).mockResolvedValue([
    { code: 'local', title: 'Local regimen', mapping_disposition: 'unmapped' },
  ]);
  vi.mocked(catalog.fetchComponents).mockResolvedValue([]);
  vi.mocked(catalog.fetchClasses).mockResolvedValue([
    { code: 'class', title: 'Unlinked class', mapping_disposition: 'classification_only',
      mapped_concept: { vocabulary_id: 'ATC', concept_code: 'L01', concept_name: 'Antineoplastic agents',
        domain_id: 'Drug', concept_class_id: 'ATC 2nd', standard_concept: 'C' } },
  ]);
});

it('shows unmapped regimens and retains the existing create control', async () => {
  render(<MemoryRouter><TherapyMappingPage /></MemoryRouter>);
  expect(await screen.findByText('Local regimen')).toBeInTheDocument();
  expect(screen.getByText('Unmapped')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /Add Regimen/ }));
  expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument();
});

it('shows an unlinked class using its actual vocabulary and classification disposition', async () => {
  render(<MemoryRouter><TherapyMappingPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: 'Component → Classes' }));
  expect(await screen.findByText('Unlinked class')).toBeInTheDocument();
  expect(screen.getByText(/ATC:L01/)).toBeInTheDocument();
  expect(screen.getByText(/Classification only/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Add Class/ })).toBeInTheDocument();
});
