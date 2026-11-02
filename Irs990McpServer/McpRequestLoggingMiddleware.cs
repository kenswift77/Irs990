using System.Diagnostics;
using System.Text.Json;
using Irs990McpServer.Data;
using Irs990McpServer.Models;

namespace Irs990McpServer.Middleware;

/// <summary>
/// Logs every MCP tool call to dbo.mcp_request_log.
///
/// How it works:
///   - SSE connections  (GET  /mcp) — skipped; no tool call is made yet.
///   - Tool-call POSTs  (POST /mcp) — body is buffered, JSON-RPC method
///     and tool name are extracted, then the row is inserted after the
///     response completes.
///
/// Must be registered AFTER ApiKeyMiddleware so HttpContext.Items already
/// contains the authenticated McpClient.
/// </summary>
public sealed class McpRequestLoggingMiddleware(
    RequestDelegate     next,
    McpClientRepository clientRepo,
    ILogger<McpRequestLoggingMiddleware> logger)
{
    public async Task InvokeAsync(HttpContext context)
    {
        // Only log POST requests to /mcp — that's where tool calls arrive.
        // Skip GET (SSE connection) and /health.
        if (!context.Request.Path.StartsWithSegments("/mcp")
            || !HttpMethods.IsPost(context.Request.Method))
        {
            await next(context);
            return;
        }

        // ----------------------------------------------------------------
        // Buffer the request body so we can read it AND pass it on.
        // ----------------------------------------------------------------
        context.Request.EnableBuffering();

        var sw = Stopwatch.StartNew();
        string? toolName   = null;
        string  status     = "success";
        string? errMessage = null;

        try
        {
            // Peek at the JSON body to extract the tool name.
            toolName = await ExtractToolNameAsync(context.Request, context.RequestAborted);

            await next(context);

            if (context.Response.StatusCode >= 400)
            {
                status     = "error";
                errMessage = $"HTTP {context.Response.StatusCode}";
            }
        }
        catch (Exception ex)
        {
            status     = "error";
            errMessage = ex.Message;
            throw;   // re-throw so the MCP framework handles it
        }
        finally
        {
            sw.Stop();
            await WriteLogRowAsync(context, toolName, sw.Elapsed, status, errMessage);
        }
    }

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    /// <summary>
    /// Reads the JSON-RPC body and returns the tool name when the method is
    /// "tools/call". Returns null for any other method or parse error.
    /// </summary>
    private static async Task<string?> ExtractToolNameAsync(
        HttpRequest request, CancellationToken ct)
    {
        try
        {
            request.Body.Position = 0;
            using var doc = await JsonDocument.ParseAsync(request.Body, cancellationToken: ct);
            request.Body.Position = 0;   // rewind for the actual handler

            if (!doc.RootElement.TryGetProperty("method", out var method))
                return null;

            if (method.GetString() != "tools/call")
                return method.GetString();  // log other methods too (e.g. "initialize")

            // tools/call → params.name
            if (doc.RootElement.TryGetProperty("params", out var p)
                && p.TryGetProperty("name", out var name))
            {
                return name.GetString();
            }
        }
        catch
        {
            // Body wasn't JSON, or was empty — not an error we care about
        }

        return null;
    }

    private async Task WriteLogRowAsync(
        HttpContext context,
        string?     toolName,
        TimeSpan    elapsed,
        string      status,
        string?     errMessage)
    {
        // Only log if authentication succeeded (client will be in Items)
        if (context.Items["McpClient"] is not McpClient client)
            return;

        var clientUser     = context.Items["McpClientUser"] as McpClientUser;
        var userIdentifier = context.Items["McpUserId"]     as string;
        var ip             = context.Connection.RemoteIpAddress?.ToString();

        await clientRepo.LogRequestAsync(
            clientId:       client.Id,
            clientUserId:   clientUser?.Id,
            userIdentifier: userIdentifier,
            toolName:       toolName,
            durationMs:     (int)elapsed.TotalMilliseconds,
            status:         status,
            ipAddress:      ip,
            errorMessage:   errMessage,
            ct:             CancellationToken.None   // don't cancel logging on disconnect
        );

        logger.LogDebug(
            "MCP log: client={Client} user={User} tool={Tool} {Duration}ms {Status}",
            client.ClientName, userIdentifier ?? "-", toolName ?? "-",
            (int)elapsed.TotalMilliseconds, status);
    }
}
