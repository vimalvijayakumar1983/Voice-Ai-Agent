'use strict';

const PAGE_TITLES = Object.freeze({
  '/': 'Overview',
  '/agents': 'Voice agents',
  '/knowledge': 'Knowledge Studio',
  '/playground': 'Voice playground',
  '/calls': 'Conversations',
  '/workflows': 'Workflows',
  '/campaigns': 'Campaigns',
  '/compliance': 'Compliance',
  '/integrations': 'Integrations',
  '/billing': 'Cost & call reports',
  '/settings': 'Workspace settings',
});

const E164_PATTERN = '\\+[1-9][0-9]{7,14}';

function pageTitleForPath(pathname) {
  return PAGE_TITLES[pathname] || 'Workspace';
}

function focusTrapTarget(activeIndex, totalItems, shiftKey) {
  if (totalItems <= 0) return null;
  if (activeIndex < 0) return shiftKey ? totalItems - 1 : 0;
  if (shiftKey && activeIndex === 0) return totalItems - 1;
  if (!shiftKey && activeIndex === totalItems - 1) return 0;
  return null;
}

module.exports = {
  E164_PATTERN,
  PAGE_TITLES,
  focusTrapTarget,
  pageTitleForPath,
};
