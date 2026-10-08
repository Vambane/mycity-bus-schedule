/**
 * Journey map integration for transfer routes
 */

function showJourneyMap(button) {
  // Get the map container for this connection
  const card = button.closest('.card');
  const mapContainer = card.querySelector('.journey-map-container');

  if (mapContainer.classList.contains('hidden')) {
    // Show the map
    mapContainer.classList.remove('hidden');
    button.textContent = 'Hide Map';
  } else {
    // Hide the map
    mapContainer.classList.add('hidden');
    button.textContent = 'Show Route Map';
  }
}
