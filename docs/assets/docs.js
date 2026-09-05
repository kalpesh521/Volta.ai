(function () {
  const sidebar = document.querySelector(".sidebar");
  const menuBtn = document.querySelector(".menu-btn");
  const search = document.querySelector(".search");

  if (menuBtn && sidebar) {
    menuBtn.addEventListener("click", function () {
      sidebar.classList.toggle("open");
    });
  }

  if (search) {
    search.addEventListener("input", function () {
      const q = search.value.trim().toLowerCase();
      document.querySelectorAll(".doc section, article section, .searchable").forEach(function (block) {
        const text = block.textContent.toLowerCase();
        block.style.display = !q || text.includes(q) ? "" : "none";
      });
    });
  }

  const path = location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll(".nav a").forEach(function (link) {
    const href = link.getAttribute("href");
    if (href === path || (path === "" && href === "index.html")) {
      link.classList.add("active");
    }
  });
})();
