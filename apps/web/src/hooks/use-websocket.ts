'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { io, type Socket } from 'socket.io-client';
import { useAuthStore } from '@/stores/auth-store';

interface UseWebSocketOptions {
  namespace?: string;
  autoConnect?: boolean;
}

export function useWebSocket(options: UseWebSocketOptions = {}) {
  const { namespace = '', autoConnect = true } = options;
  const { token } = useAuthStore();
  const socketRef = useRef<Socket | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    if (!autoConnect || !token) return;

    const url = `${process.env.NEXT_PUBLIC_WS_URL || 'http://localhost:3001'}${namespace}`;
    const socket = io(url, {
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionAttempts: 5,
      reconnectionDelay: 1000,
    });

    socket.on('connect', () => setIsConnected(true));
    socket.on('disconnect', () => setIsConnected(false));

    socketRef.current = socket;

    return () => {
      socket.disconnect();
      socketRef.current = null;
      setIsConnected(false);
    };
  }, [token, namespace, autoConnect]);

  const emit = useCallback(
    (event: string, data?: unknown) => {
      socketRef.current?.emit(event, data);
    },
    []
  );

  const on = useCallback(
    (event: string, handler: (...args: unknown[]) => void) => {
      socketRef.current?.on(event, handler);
      return () => {
        socketRef.current?.off(event, handler);
      };
    },
    []
  );

  const off = useCallback(
    (event: string, handler?: (...args: unknown[]) => void) => {
      socketRef.current?.off(event, handler);
    },
    []
  );

  return { socket: socketRef.current, isConnected, emit, on, off };
}
