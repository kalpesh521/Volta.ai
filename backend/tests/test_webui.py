"""Temporary energy HTML console is served from the API process."""


async def test_energy_console_html(client):
    response = await client.get("/ui/energy")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert b"Energy API console" in response.content
    assert b"/energy/me/live" in response.content


async def test_energy_console_aliases(client):
    for path in ("/ui", "/ui/energy-console.html"):
        response = await client.get(path)
        assert response.status_code == 200, path
