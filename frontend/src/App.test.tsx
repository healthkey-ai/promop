import React from 'react';
import { render, screen } from '@testing-library/react';
import App from './App';

jest.mock('./hooks/useAuth', () => ({
  useAuth: () => ({
    currentUser: null,
    loading: false,
    refresh: jest.fn(),
  }),
}));

jest.mock('./components/Auth/Login', () => ({
  Login: () => <div>Login Page</div>,
}));

jest.mock('./components/Auth/AuthCallback', () => ({
  AuthCallback: () => <div>Auth Callback</div>,
}));

jest.mock('./components/Patient/PatientList', () => () => <div>Patient List</div>);
jest.mock('./components/Patient/PatientDetail', () => () => <div>Patient Detail</div>);
jest.mock('./components/Patient/UploadFHIR', () => () => <div>Upload FHIR</div>);
jest.mock('./components/Patient/UploadCSV', () => () => <div>Upload CSV</div>);

test('redirects unauthenticated users to login', () => {
  window.history.pushState({}, 'Test page', '/');
  render(<App />);
  expect(screen.getByText('Login Page')).toBeInTheDocument();
});
