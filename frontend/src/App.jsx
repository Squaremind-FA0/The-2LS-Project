import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

function Login() {
  return <h1>Login</h1>;
}

function Worker() {
  return <h1>Worker</h1>;
}

function Admin() {
  return <h1>Admin</h1>;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/worker/*" element={<Worker />} />
        <Route path="/admin/*" element={<Admin />} />
        <Route path="/" element={<Navigate to="/login" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
