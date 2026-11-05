import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, auth, me, UNAUTHORIZED_EVENT } from '../api/client.js';

const TOKEN_KEY = 'token';

export const AuthContext = createContext(null);

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error('useAuth must be used inside an AuthProvider');
  }
  return value;
}

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState(null);
  // True while a stored token is being checked, so a reload doesn't bounce
  // through /login before the session is known.
  const [loading, setLoading] = useState(() => Boolean(localStorage.getItem(TOKEN_KEY)));
  // Bumped by every login/logout: a slower in-flight bootstrap must not
  // overwrite whatever happened after it started.
  const session = useRef(0);

  const logout = useCallback(() => {
    session.current += 1;
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
    setLoading(false);
  }, []);

  const login = useCallback(
    async (email, password) => {
      session.current += 1;
      const started = session.current;
      setLoading(false);
      const { access_token: accessToken } = await auth.login(email, password);
      localStorage.setItem(TOKEN_KEY, accessToken);
      try {
        const currentUser = await me.get();
        if (session.current === started) {
          setToken(accessToken);
          setUser(currentUser);
        }
      } catch (error) {
        logout();
        throw error;
      }
    },
    [logout],
  );

  useEffect(() => {
    if (!localStorage.getItem(TOKEN_KEY)) {
      return undefined;
    }
    const started = session.current;
    me.get()
      .then((currentUser) => {
        if (session.current === started) {
          setUser(currentUser);
          setLoading(false);
        }
      })
      .catch((error) => {
        if (session.current !== started) {
          return;
        }
        // Only the server saying no ends the session; a network blip keeps the
        // token so the next reload can retry it.
        if (error instanceof ApiError && error.status === 401) {
          logout();
        } else {
          setLoading(false);
        }
      });
    return undefined;
  }, [logout]);

  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, logout);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, logout);
  }, [logout]);

  const value = useMemo(
    () => ({ token, user, loading, login, logout }),
    [token, user, loading, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
