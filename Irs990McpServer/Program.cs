using Irs990McpServer.Data;
using Irs990McpServer.Middleware;

var builder = WebApplication.CreateBuilder(args);

// -----------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------
builder.Configuration
    .AddJsonFile("appsettings.json", optional: false)
    .AddJsonFile($"appsettings.{builder.Environment.EnvironmentName}.json", optional: true)
    .AddEnvironmentVariables()
    .AddUserSecrets<Program>(optional: true);   // local dev only

// -----------------------------------------------------------------------
// Logging / Application Insights
// -----------------------------------------------------------------------
builder.Logging.ClearProviders();
builder.Logging.AddConsole();

var aiConnStr = builder.Configuration["ApplicationInsights:ConnectionString"];
if (!string.IsNullOrWhiteSpace(aiConnStr))
{
    builder.Services.AddApplicationInsightsTelemetry(opts =>
        opts.ConnectionString = aiConnStr);
}

// -----------------------------------------------------------------------
// Data layer
// -----------------------------------------------------------------------
builder.Services.AddSingleton<McpClientRepository>();  // API key auth + request logging

// -----------------------------------------------------------------------
// MCP server — SSE transport (required for Azure App Service)
// -----------------------------------------------------------------------
builder.Services
    .AddMcpServer(opts =>
    {
        opts.ServerInfo = new ModelContextProtocol.Protocol.Implementation
        {
            Name    = "IRS990McpServer",
            Version = "1.0.0"
        };
    })
    .WithHttpTransport()               // SSE / HTTP — no stdin/stdout needed
    .WithToolsFromAssembly();          // discovers all [McpServerToolType] classes

// -----------------------------------------------------------------------
// Health checks (for Azure App Service health probes)
// -----------------------------------------------------------------------
builder.Services.AddHealthChecks()
    .AddCheck("self", () => Microsoft.Extensions.Diagnostics.HealthChecks.HealthCheckResult.Healthy());

// -----------------------------------------------------------------------
// Build & configure pipeline
// -----------------------------------------------------------------------
var app = builder.Build();

app.UseRouting();

// 1. API key validation — resolves client + user, stores in HttpContext.Items.
//    /health is exempted inside the middleware.
app.UseMiddleware<ApiKeyMiddleware>();

// 2. Request logging — reads tool name from JSON-RPC body, logs after response.
//    Registered after auth so the client identity is already in Items.
app.UseMiddleware<McpRequestLoggingMiddleware>();

// Health-check endpoint used by App Service and load balancers
app.MapHealthChecks("/health");

// MCP SSE endpoint
app.MapMcp("/mcp");

// Root redirect so a browser hit isn't a 404
app.MapGet("/", () => Results.Redirect("/health"));

app.Logger.LogInformation(
    "IRS 990 MCP server starting on {Env}",
    app.Environment.EnvironmentName);

app.Run();
