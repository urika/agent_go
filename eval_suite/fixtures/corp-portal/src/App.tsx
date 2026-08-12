import { BrowserRouter, Routes, Route, NavLink, Link } from "react-router-dom";
import { NAV_ITEMS } from "./lib/navigation";

export default function App() {
  return (
    <BrowserRouter>
      <header className="site-header">
        <nav className="site-nav">
          <Link to="/" className="logo">企业门户</Link>
          <ul>
            {NAV_ITEMS.map((item) => (
              <li key={item.path}>
                <NavLink
                  to={item.path}
                  end={item.path === "/"}
                  className={({ isActive }) => (isActive ? "active" : "")}
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="site-main">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/products" element={<ProductsPage />} />
          <Route path="/news" element={<NewsPage />} />
          <Route path="/about" element={<AboutPage />} />
          <Route path="/contact" element={<ContactPage />} />
        </Routes>
      </main>
      <footer className="site-footer">
        <p>© 2026 企业门户 · 保留所有权利</p>
      </footer>
    </BrowserRouter>
  );
}

function HomePage() {
  return (
    <section>
      <h1>欢迎访问企业门户</h1>
      <p>我们专注于工业数字化转型解决方案。</p>
    </section>
  );
}

function ProductsPage() {
  return <section><h1>产品中心</h1></section>;
}

function NewsPage() {
  return <section><h1>新闻中心</h1></section>;
}

function AboutPage() {
  return <section><h1>关于我们</h1></section>;
}

function ContactPage() {
  return <section><h1>联系我们</h1></section>;
}
