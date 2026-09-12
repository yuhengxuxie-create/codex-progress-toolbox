using System.Net;
using System.Text;
using System.Text.Json;
using TreasureChest.Services;

internal static class UpdateRateLimitRegression
{
    private sealed class Handler(Func<HttpRequestMessage, HttpResponseMessage> reply) : HttpMessageHandler
    {
        public int ApiCalls, ManifestCalls;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            if (request.RequestUri!.Host == "api.github.com") ApiCalls++; else ManifestCalls++;
            var result = reply(request); result.RequestMessage = request;
            return Task.FromResult(result);
        }
    }
    private static HttpResponseMessage Json(string body, HttpStatusCode status = HttpStatusCode.OK) =>
        new(status) { Content = new StringContent(body, Encoding.UTF8, "application/json") };
    private static string Manifest(string? url = null, string digest = "", string version = "1.6.2")
    {
        var name = $"codex-feishu-ecosystem-v{version}-upgrade-from-v1.x.zip";
        return JsonSerializer.Serialize(new { schema_version = 1, ecosystem_version = version,
            release_tag = "v" + version, release_notes_url = EcosystemUpdateService.RepositoryWebRoot + "/releases/tag/v" + version,
            minimum_upgradable_version = "1.5.0", packages = new { upgrade = new { name,
                url = url ?? EcosystemUpdateService.RepositoryWebRoot + "/releases/download/v" + version + "/" + name,
                sha256 = digest == "" ? new string('a',64) : digest, size = 123 } } });
    }
    public static async Task VerifyAsync(string root, Action<bool,string> assert)
    {
        var now = new DateTimeOffset(2026,9,12,12,0,0,TimeSpan.Zero);
        var retryAt = now.AddHours(2);
        HttpResponseMessage Limited()
        {
            var r = Json("{\"message\":\"API rate limit exceeded\"}", HttpStatusCode.Forbidden);
            r.Headers.TryAddWithoutValidation("Retry-After", "60");
            r.Headers.TryAddWithoutValidation("X-RateLimit-Remaining", "0");
            r.Headers.TryAddWithoutValidation("X-RateLimit-Reset", retryAt.ToUnixTimeSeconds().ToString());
            return r;
        }
        var path = Path.Combine(root,"update-limits.json");
        var handler = new Handler(r => r.RequestUri!.Host == "api.github.com" ? Limited() : Json("\uFEFF" + Manifest()));
        using var client = new HttpClient(handler);
        using (var service = new EcosystemUpdateService(client,path,()=>now))
        {
            var results = await Task.WhenAll(Enumerable.Range(0,8).Select(_=>service.CheckAsync(new Version(1,6,0))));
            assert(results.All(r=>r.IsUpdateAvailable && r.Release!.Package.Size==123),"官方清单回退未给可校验升级包");
            assert(handler.ApiCalls==1 && handler.ManifestCalls==1,"并发/重复检查未共用缓存");
        }
        using(var restored = new EcosystemUpdateService(client,path,()=>now))
        {
            try { await restored.CheckAsync(new Version(1,6,0)); throw new Exception("restart bypass"); }
            catch(UpdateRateLimitException e){assert(e.RetryAt==retryAt,"未保留两个响应头的最晚时间");}
            assert(handler.ApiCalls==1 && handler.ManifestCalls==1,"重启绕过持久退避");
        }
        now=now.AddMinutes(6);
        using(var restored=new EcosystemUpdateService(client,path,()=>now))
        {
            await restored.CheckAsync(new Version(1,6,0));
            assert(handler.ApiCalls==1 && handler.ManifestCalls==2,"冷却中访问API或无法重新读取官方清单");
        }
        now=retryAt.AddSeconds(1);
        using(var restored=new EcosystemUpdateService(client,path,()=>now))
        {
            await restored.CheckAsync(new Version(1,6,0));
            assert(handler.ApiCalls==2,"到期未允许重新检查API");
        }
        foreach(var bad in new[]{Manifest("https://evil.invalid/package.zip"),Manifest(digest:"bad"),Manifest(version:"1.6.2-preview"),
            Manifest().Replace("\"release_tag\":\"v1.6.2\"", "\"release_tag\":\"v1.6.3\""),
            Manifest().Replace("\"size\":123", "\"size\":0"), new string('x',65537)})
        {
            var h=new Handler(r=>r.RequestUri!.Host=="api.github.com"?Limited():Json(bad));
            using var c=new HttpClient(h);using var s=new EcosystemUpdateService(c,utcNow:()=>now);
            try{await s.CheckAsync(new Version(1,6,0));throw new Exception("invalid accepted");}
            catch(HttpRequestException e){assert(e.Message.Contains("官方更新清单暂不可用"),"回退失败无明确手动说明");}
            try{await s.CheckAsync(new Version(1,6,0));}catch(UpdateRateLimitException){}
            assert(h.ApiCalls==1 && h.ManifestCalls==1,"失败回退被手动连点重复请求");
        }
        var denied=new Handler(_=>Json("{\"message\":\"forbidden\"}",HttpStatusCode.Forbidden));
        var fallbackState=Path.Combine(root,"manifest-wait.json");
        var fallbackLimited=new Handler(r=>{
            if(r.RequestUri!.Host=="api.github.com")return Limited();
            var response=Json("{}",HttpStatusCode.TooManyRequests);
            response.Headers.TryAddWithoutValidation("Retry-After","7200");return response;});
        using(var c=new HttpClient(fallbackLimited))
        {
            using(var s=new EcosystemUpdateService(c,fallbackState,()=>now))
                try{await s.CheckAsync(new Version(1,6,0));}catch(HttpRequestException){}
            using var saved=JsonDocument.Parse(File.ReadAllText(fallbackState));
            assert(saved.RootElement.GetProperty("ManifestRetryAt").GetDateTimeOffset()==now.AddHours(2),"清单长等待未持久化");
            assert(saved.RootElement.GetProperty("ApiRetryAt").GetDateTimeOffset()<now.AddHours(2),"清单等待污染API等待");
            now=now.AddMinutes(6);
            using(var s=new EcosystemUpdateService(c,fallbackState,()=>now))
                try{await s.CheckAsync(new Version(1,6,0));}catch(HttpRequestException){}
            assert(fallbackLimited.ManifestCalls==1,"重启在清单Retry-After前重新请求");
        }
        using(var c=new HttpClient(denied))using(var s=new EcosystemUpdateService(c))
        {
            try{await s.CheckAsync(new Version(1,6,0));throw new Exception("denied accepted");}
            catch(HttpRequestException e){assert(e is not UpdateRateLimitException,"普通403被误称限流");}
            assert(denied.ManifestCalls==0,"普通403错误启用回退");
        }
        foreach(var header in new[]{"Sat, 12 Sep 2026 16:00:00 GMT",new string('9',100),"invalid"})
        {
            var h=new Handler(r=>{
                if(r.RequestUri!.Host!="api.github.com")return Json("invalid");
                var response=Json("{}",HttpStatusCode.TooManyRequests);
                response.Headers.TryAddWithoutValidation("Retry-After",header);return response;});
            using var c=new HttpClient(h);using var s=new EcosystemUpdateService(c,utcNow:()=>now);
            try{await s.CheckAsync(new Version(1,6,0));}catch(HttpRequestException){}
            try{await s.CheckAsync(new Version(1,6,0));throw new Exception("no wait");}
            catch(UpdateRateLimitException e){assert(e.RetryAt>now,"日期/巨大/无效等待未安全退避");
                if(header.StartsWith("999"))assert(e.RetryAt==DateTimeOffset.MaxValue,"巨大等待被缩短");}
        }
        var notModified=new Handler(_=>new HttpResponseMessage(HttpStatusCode.NotModified));
        using(var c=new HttpClient(notModified))using(var s=new EcosystemUpdateService(c))
            assert((await s.CheckAsync(new Version(1,6,0),"\"tag\"")).NotModified,"304行为发生破坏");
        var older=new Handler(r=>r.RequestUri!.Host=="api.github.com"?Limited():Json(Manifest(version:"1.5.0")));
        using(var c=new HttpClient(older))using(var s=new EcosystemUpdateService(c,utcNow:()=>now))
        {
            var old=await s.CheckAsync(new Version(1,6,0));
            assert(!old.IsUpdateAvailable && old.Release is null,"旧清单冒充新版");
        }
        var cache=new Handler(_=>Json("{\"tag_name\":\"v1.6.2\",\"draft\":false,\"prerelease\":false}"));
        using(var c=new HttpClient(cache))using(var s=new EcosystemUpdateService(c,utcNow:()=>now))
        {
            await s.CheckAsync(new Version(1,6,2));await s.CheckAsync(new Version(1,6,2));
            assert(cache.ApiCalls==1,"成功结果未缓存");
            now=now.AddMinutes(6);await s.CheckAsync(new Version(1,6,2));
            assert(cache.ApiCalls==2,"过期缓存阻止真实检查");
        }
        using var canceled=new CancellationTokenSource();canceled.Cancel();
        using(var s=new EcosystemUpdateService(client))
        {
            var before=handler.ApiCalls;
            try{await s.CheckAsync(new Version(1,6,0),cancellationToken:canceled.Token);throw new Exception("cancel ignored");}
            catch(OperationCanceledException){}
            assert(handler.ApiCalls==before,"已取消检查仍发出请求");
        }
    }
}
