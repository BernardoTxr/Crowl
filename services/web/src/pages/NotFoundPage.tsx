import { Link } from 'react-router-dom';

export const NotFoundPage = () => (
  <div className="page-shell not-found-page">
    <span aria-hidden="true">404</span>
    <p className="eyebrow">Off the monitored route</p>
    <h1>This page flew away.</h1>
    <p>Return to the Schengen explorer and choose another weekend destination.</p>
    <Link className="primary-button" to="/">Return to the map</Link>
  </div>
);
