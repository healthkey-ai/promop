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
    // The open tooltip repeats the label, so leave from the first "Analyst", the trigger's.
    fireEvent.mouseLeave(screen.getAllByText('Analyst')[0]);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    // Keyboard: focusing the link opens it and describes the link; Escape and blur close it.
    const link = screen.getByRole('link', { name: 'What the Analyst role can do' });
    fireEvent.focus(link);
    expect(link).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id);
    fireEvent.keyDown(link, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.focus(link);
    fireEvent.blur(link);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    fireEvent.click(link);
    expect(screen.getByRole('heading', { name: 'Roles' })).toBeInTheDocument();
  });

  it('renders an unknown role as plain text', () => {
    render(<MemoryRouter><RoleHint role="viewer" /></MemoryRouter>);
    expect(screen.getByText('viewer')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('keeps only the info link beside a control that already names the role', () => {
    render(<MemoryRouter><RoleHint role="doctor" hideLabel /></MemoryRouter>);
    expect(screen.queryByText('Doctor')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'What the Doctor role can do' })).toBeInTheDocument();
  });
});
