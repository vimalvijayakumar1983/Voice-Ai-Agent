'use client';

import { useRouter } from 'next/navigation';
import { useAuthStore } from '@/stores/auth-store';
import api from '@/lib/api';
import { setToken, removeToken } from '@/lib/auth';

interface LoginCredentials {
  email: string;
  password: string;
}

interface RegisterData {
  companyName: string;
  adminName: string;
  email: string;
  password: string;
  industry: string;
}

export function useAuth() {
  const router = useRouter();
  const { user, isAuthenticated, login: setAuth, logout: clearAuth } = useAuthStore();

  const login = async (credentials: LoginCredentials) => {
    const { data } = await api.post('/api/v1/auth/login', credentials);
    setToken(data.token);
    setAuth(data.user, data.token);
    router.push('/dashboard');
    return data;
  };

  const register = async (registerData: RegisterData) => {
    const { data } = await api.post('/api/v1/auth/register', registerData);
    setToken(data.token);
    setAuth(data.user, data.token);
    router.push('/dashboard');
    return data;
  };

  const logout = () => {
    removeToken();
    clearAuth();
    router.push('/login');
  };

  return {
    user,
    isAuthenticated,
    login,
    register,
    logout,
  };
}
