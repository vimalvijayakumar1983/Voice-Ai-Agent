declare const shellAccessibility: {
  E164_PATTERN: string;
  PAGE_TITLES: Readonly<Record<string, string>>;
  focusTrapTarget: (activeIndex: number, totalItems: number, shiftKey: boolean) => number | null;
  pageTitleForPath: (pathname: string) => string;
};

export = shellAccessibility;
