import BrandLogo from './BrandLogo';

/** Masthead for authentication and patient-record views without a page title. */
export default function BrandHeader() {
  return (
    <header className="brand-masthead flex h-24 items-center border-b border-border bg-background px-4 sm:px-6">
      <BrandLogo />
    </header>
  );
}
