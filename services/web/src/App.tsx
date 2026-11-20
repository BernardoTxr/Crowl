import { Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { CityPage } from './pages/CityPage';
import { HomePage } from './pages/HomePage';
import { NotFoundPage } from './pages/NotFoundPage';
import { StatusPage } from './pages/StatusPage';

export const CrowlRoutes = () => (
  <Routes>
    <Route element={<Layout />}>
      <Route index element={<HomePage />} />
      <Route path="cities/:slug" element={<CityPage />} />
      <Route path="status" element={<StatusPage />} />
      <Route path="home" element={<Navigate replace to="/" />} />
      <Route path="*" element={<NotFoundPage />} />
    </Route>
  </Routes>
);
