window.reaicrmSidebar = (initialGroup) => ({
  sidebarOpen: false,
  sidebarCollapsed: document.documentElement.classList.contains('sidebar-collapsed'),
  isDesktop: window.matchMedia('(min-width: 992px)').matches,
  openGroup: initialGroup,

  isRail() {
    return this.isDesktop && this.sidebarCollapsed;
  },

  setSidebarCollapsed(collapsed) {
    this.sidebarCollapsed = collapsed;
    document.documentElement.classList.toggle('sidebar-collapsed', collapsed);
    try {
      window.localStorage.setItem('reaicrm.sidebar.collapsed', String(collapsed));
    } catch (_) {
      // Browsers with disabled storage still keep the choice for this page.
    }
  },

  toggleGroup(group) {
    if (this.isRail()) {
      this.setSidebarCollapsed(false);
      this.openGroup = group;
      return;
    }
    this.openGroup = this.openGroup === group ? null : group;
  },
});
