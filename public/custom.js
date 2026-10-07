// custom.js - Add signup link to Chainlit login page

function addSignupLink() {
    // Only run on login page
    if (window.location.pathname !== '/login' && window.location.pathname !== '/login/') return;
    
    // Find the login form container
    const form = document.querySelector('form');
    if (form && !document.getElementById('custom-signup-link')) {
        const linkContainer = document.createElement('div');
        linkContainer.id = 'custom-signup-link';
        linkContainer.style.textAlign = 'center';
        linkContainer.style.marginTop = '16px';
        linkContainer.style.fontSize = '14px';
        
        const link = document.createElement('a');
        link.href = '/signup';
        link.textContent = "Don't have an account? Sign up";
        link.style.color = '#d81b60'; // Match the magenta button color
        link.style.textDecoration = 'none';
        
        link.addEventListener('mouseover', () => link.style.textDecoration = 'underline');
        link.addEventListener('mouseout', () => link.style.textDecoration = 'none');
        
        linkContainer.appendChild(link);
        
        // Add it right after the form
        form.parentNode.insertBefore(linkContainer, form.nextSibling);
    }
}

// Watch for DOM changes since Chainlit uses React and renders the form dynamically
const observer = new MutationObserver((mutations) => {
    if (window.location.pathname.startsWith('/login')) {
        addSignupLink();
    }
});

observer.observe(document.body, { childList: true, subtree: true });

// Initial check
document.addEventListener('DOMContentLoaded', addSignupLink);
