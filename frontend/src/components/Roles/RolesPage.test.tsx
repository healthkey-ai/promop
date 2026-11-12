import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import RoleHint from './RoleHint';
import RolesPage from './RolesPage';

describe('RolesPage (#2473)', () => {
  it('describes every role with what it can and cannot do', () => {
    render(<MemoryRouter><RolesPage /></MemoryRouter>);
    for (const name of ['Org Admin', 'Doctor', 'Analyst', 'Patient']) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    }
    // Doctor and Analyst both lack approval; it stays with org admins and staff.
    expect(screen.getAllByText(/Approve a mapping; that stays with org admins and staff/)).toHaveLength(2);
    expect(screen.getByText(/See any patient's data/)).toBeInTheDocument();
    expect(screen.getByText(/Roles are additive/)).toBeInTheDocument();
  });
});

describe('RoleHint', () => {
  it('shows the role summary on hover and links to the roles page', () => {
    render(
      <MemoryRouter initialEntries={['/x']}>
        <Routes>
          <Route path="/x" element={<RoleHint role="analyst" />} />
          <Route path="/roles" element={<RolesPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.mouseEnter(screen.getByText('Analyst'));
    expect(screen.getByRole('tooltip')).toHaveTextContent(/proposes them; sees no individual patient data/);
    fireEvent.click(screen.getByRole('link', { name: 'What a Analyst can do' }));
    expect(screen.getByRole('heading', { name: 'Roles' })).toBeInTheDocument();
  });

  it('renders an unknown role as plain text', () => {
    render(<MemoryRouter><RoleHint role="viewer" /></MemoryRouter>);
    expect(screen.getByText('viewer')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
