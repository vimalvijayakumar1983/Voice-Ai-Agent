import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AppState {
  sidebarCollapsed: boolean;
  currentTenantId: string | null;
  currentTenantName: string | null;
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setCurrentTenant: (id: string, name: string) => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      currentTenantId: null,
      currentTenantName: null,
      toggleSidebar: () =>
        set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
      setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
      setCurrentTenant: (id, name) =>
        set({ currentTenantId: id, currentTenantName: name }),
    }),
    {
      name: 'voice-ai-app',
    }
  )
);
