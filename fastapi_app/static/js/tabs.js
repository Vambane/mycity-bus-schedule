/**
 * Tab switching functionality
 */

function switchTab(tabName) {
  // Remove active class from all tabs
  document.querySelectorAll('.tab').forEach(tab => {
    tab.classList.remove('active');
  });

  // Hide all tab content
  document.querySelectorAll('.tab-content').forEach(content => {
    content.classList.remove('active');
  });

  // Activate clicked tab
  event.target.classList.add('active');

  // Show corresponding content
  const contentId = `tab-${tabName}`;
  const content = document.getElementById(contentId);
  if (content) {
    content.classList.add('active');
  }
}
