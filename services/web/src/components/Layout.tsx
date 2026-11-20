import { useEffect } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { joinClassNames } from '../utils';

const CrowMark = () => (
  <svg viewBox="0 0 64 64" aria-hidden="true" className="brand-mark">
    <rect width="64" height="64" rx="15" fill="currentColor" />
    <path d="M13 42c7-1 11-6 12-13-2-5-1-10 4-14 1 6 5 9 11 10l11 3-8 5c-1 10-7 16-18 16-5 0-9-2-12-7Z" fill="#65b8e8" />
    <circle cx="35" cy="23" r="2" fill="#242421" />
    <path d="m24 45-3 7m12-7 3 7" stroke="#f3d37b" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

export const Layout = () => {
  const location = useLocation();

  useEffect(() => {
    document.getElementById('main-content')?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'instant' });
  }, [location.pathname]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <header className="site-header">
        <div className="header-inner">
          <Link className="brand" to="/" aria-label="Crowl home">
            <CrowMark />
            <span className="brand-word">Crowl<span aria-hidden="true">.</span></span>
            <span className="brand-tagline">Weekend travel radar</span>
          </Link>
          <nav aria-label="Main navigation" className="main-nav">
            <NavLink to="/" end className={({ isActive }) => joinClassNames('nav-link', isActive && 'is-active')}>
              <span aria-hidden="true">⌖</span> Explore
            </NavLink>
            <NavLink to="/status" className={({ isActive }) => joinClassNames('nav-link', isActive && 'is-active')}>
              <span aria-hidden="true">◉</span> Source status
            </NavLink>
          </nav>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}>
        <Outlet />
      </main>
      <footer className="site-footer">
        <div>
          <strong>Crowl</strong> monitors weekend fares from Paris to the Schengen Area.
        </div>
        <div>Prices are observations, not guarantees. Always recheck with the seller.</div>
      </footer>
    </div>
  );
};

export const DemoNotice = ({ compact = false, kind = 'fallback' }: { compact?: boolean; kind?: 'fallback' | 'synthetic' | 'catalog' | 'catalog-synthetic' }) => (
  <div className={joinClassNames('demo-notice', compact && 'is-compact')} role="status">
    <span aria-hidden="true">✦</span>
    <span>
      <strong>{kind === 'catalog-synthetic' ? 'Mock catalog + synthetic fares' : kind === 'synthetic' ? 'Contains synthetic seed data' : kind === 'catalog' ? 'Mock city catalog' : 'Preview data'}</strong>
      {compact ? '' : kind === 'catalog-synthetic' ? ' — city facts, photos, journey times, and generated prices remain examples until the final JSON and live extractors arrive.' : kind === 'synthetic' ? ' — generated development fares are clearly marked and are not live prices.' : kind === 'catalog' ? ' — city facts, photos, and journey-time samples remain examples until the final JSON arrives.' : ' — the API is unavailable, so Crowl is showing a realistic sample.'}
    </span>
  </div>
);

export const LoadingBlock = ({ label = 'Loading Crowl data…' }: { label?: string }) => (
  <div className="loading-block" role="status">
    <span className="loading-crow" aria-hidden="true">●</span>
    <span>{label}</span>
  </div>
);

export const ErrorBlock = ({ message }: { message: string }) => (
  <div className="error-block" role="alert">
    <strong>We could not load this view.</strong>
    <span>{message}</span>
  </div>
);
