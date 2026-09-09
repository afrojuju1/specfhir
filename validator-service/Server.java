import com.google.gson.*;
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;
import org.hl7.fhir.r5.elementmodel.Manager.FhirFormat;
import org.hl7.fhir.r5.formats.JsonParser;
import org.hl7.fhir.r5.model.StructureDefinition;
import org.hl7.fhir.r5.renderers.RendererFactory;
import org.hl7.fhir.utilities.ByteProvider;
import org.hl7.fhir.utilities.TimeTracker;
import org.hl7.fhir.utilities.http.ManagedWebAccess;
import org.hl7.fhir.validation.ValidationEngine;
import org.hl7.fhir.validation.service.ValidationService;
import org.hl7.fhir.validation.service.IPackageInstaller;
import org.hl7.fhir.validation.service.StandAloneValidatorFetcher;
import org.hl7.fhir.validation.service.model.*;

/** Transport and lifecycle only. FHIR semantics remain in the pinned HL7 engine. */
public final class Server {
    static final Gson JSON = new Gson();
    static final int MAX_INPUT = 10 * 1024 * 1024, MAX_OUTPUT = 16 * 1024 * 1024;
    static final Semaphore ADMISSION = new Semaphore(9);
    static final ReentrantLock ENGINE_LOCK = new ReentrantLock(true);
    static final ScheduledExecutorService WATCHDOG = Executors.newSingleThreadScheduledExecutor();
    static final LinkedHashMap<String, ValidationEngine> ENGINES = new LinkedHashMap<>(4, .75f, true);
    static final AtomicLong BUILDS = new AtomicLong(), COMPLETED = new AtomicLong(), EVICTIONS = new AtomicLong();
    static volatile boolean ready;
    static volatile List<String> cached = List.of();
    static JsonObject manifest;
    static String mode, endpoint;

    static class Builder extends ValidationService {
        Builder() { super(new RendererFactory()); }
        ValidationEngine build(String context) throws Exception {
            var options = new ValidationEngineParameters().setSv("4.0.1").setTxServer(endpoint)
                .setLocale(Locale.US).setCheckReferences(true);
            if (!context.equals("hl7.fhir.r4.core#4.0.1")) options.addIg(context);
            var result = buildValidationEngine(options, new InstanceValidatorParameters(),
                "hl7.fhir.r4.core#4.0.1", new TimeTracker());
            // Context setup loads the exact closure; instance URLs cannot add packages.
            var fetcher = new StandAloneValidatorFetcher(result.getPcm(), result.getContext(), new IPackageInstaller() {
                public boolean packageExists(String id, String version) { return false; }
                public void loadPackage(String id, String version) { throw new IllegalStateException("Package context is frozen"); }
            });
            result.setFetcher(fetcher);
            result.getContext().setLocator(fetcher);
            return result;
        }
    }

    static String sha256(Path path) throws Exception {
        var md = MessageDigest.getInstance("SHA-256");
        try (InputStream input = Files.newInputStream(path)) {
            byte[] buffer = new byte[65536];
            int n;
            while ((n = input.read(buffer)) != -1) md.update(buffer, 0, n);
        }
        return HexFormat.of().formatHex(md.digest());
    }

    static Set<String> strings(JsonArray values) {
        Set<String> result = new TreeSet<>();
        for (var value : values) result.add(value.getAsString());
        return result;
    }

    static Set<String> loaded(ValidationEngine engine) {
        String summary = engine.getContext().loadedPackageSummary();
        return new TreeSet<>(Arrays.asList(summary.substring(1, summary.length() - 1).split(", ")));
    }

    static ValidationEngine engine(String context) throws Exception {
        if (ENGINES.containsKey(context)) return ENGINES.get(context);
        if (!manifest.getAsJsonObject("contexts").has(context)) throw new IllegalArgumentException();
        if (ENGINES.size() == 2) {
            ENGINES.remove(ENGINES.keySet().iterator().next());
            EVICTIONS.incrementAndGet();
        }
        var result = new Builder().build(context);
        if (!loaded(result).equals(strings(manifest.getAsJsonObject("contexts").getAsJsonArray(context))))
            throw new IllegalStateException("Loaded package mismatch");
        ENGINES.put(context, result);
        BUILDS.incrementAndGet();
        cached = List.copyOf(ENGINES.keySet());
        return result;
    }

    static void send(HttpExchange exchange, int status, Object value) throws IOException {
        byte[] bytes = JSON.toJson(value).getBytes(StandardCharsets.UTF_8);
        if (bytes.length > MAX_OUTPUT) {
            status = 500;
            bytes = "{\"error\":\"Validator output exceeds limit\"}".getBytes(StandardCharsets.UTF_8);
        }
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        if (status == 429) exchange.getResponseHeaders().set("Retry-After", "1");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var output = exchange.getResponseBody()) { output.write(bytes); }
    }

    static void profiles(JsonElement value, ValidationEngine engine) {
        if (value.isJsonObject()) {
            var obj = value.getAsJsonObject();
            if (obj.has("meta") && obj.get("meta").isJsonObject()) {
                var meta = obj.getAsJsonObject("meta");
                if (meta.has("profile")) for (var p : meta.getAsJsonArray("profile"))
                    if (engine.getContext().fetchResource(StructureDefinition.class, p.getAsString()) == null)
                        throw new IllegalArgumentException();
            }
            for (var entry : obj.entrySet()) profiles(entry.getValue(), engine);
        } else if (value.isJsonArray()) {
            for (var child : value.getAsJsonArray()) profiles(child, engine);
        }
    }

    static void validate(HttpExchange exchange) throws IOException {
        if (!exchange.getRequestMethod().equals("POST")) { send(exchange, 405, Map.of("error", "POST required")); return; }
        if (!ready) { send(exchange, 503, Map.of("error", "Validator is warming")); return; }
        if (!ADMISSION.tryAcquire()) { send(exchange, 429, Map.of("error", "Validator queue full")); return; }
        boolean locked = false;
        ScheduledFuture<?> deadline = null;
        String context = null;
        try {
            byte[] body = exchange.getRequestBody().readNBytes(24 * 1024 * 1024 + 1);
            if (body.length > 24 * 1024 * 1024) { send(exchange, 413, Map.of("error", "Request too large")); return; }
            var request = com.google.gson.JsonParser.parseString(new String(body, StandardCharsets.UTF_8)).getAsJsonObject();
            if (!request.get("snapshot_id").getAsString().equals(manifest.get("snapshot_id").getAsString())) {
                send(exchange, 409, Map.of("error", "Stale validator snapshot; run validator-setup and recreate service")); return;
            }
            if (!request.get("terminology_mode").getAsString().equals(mode)) {
                send(exchange, 409, Map.of("error", "Terminology mode differs from service")); return;
            }
            context = request.get("package").getAsString();
            if (!manifest.getAsJsonObject("contexts").has(context)) { send(exchange, 404, Map.of("error", "Package context is not provisioned")); return; }
            byte[] instance = request.get("instance").getAsString().getBytes(StandardCharsets.UTF_8);
            if (instance.length > MAX_INPUT) { send(exchange, 413, Map.of("error", "Instance too large")); return; }
            var parsed = com.google.gson.JsonParser.parseString(new String(instance, StandardCharsets.UTF_8)).getAsJsonObject();
            if (!parsed.has("resourceType")) throw new IllegalArgumentException();
            int seconds = request.get("timeout_seconds").getAsInt();
            if (seconds < 1 || seconds > 600) throw new IllegalArgumentException();
            locked = ENGINE_LOCK.tryLock(Math.min(seconds, 10), TimeUnit.SECONDS);
            if (!locked) { send(exchange, 429, Map.of("error", "Validator queue wait exceeded")); return; }
            // A stuck engine is not safely cancellable with Thread.interrupt(). Recreate the process.
            deadline = WATCHDOG.schedule(() -> Runtime.getRuntime().halt(124), Math.min(seconds, 150), TimeUnit.SECONDS);
            var current = engine(context);
            profiles(parsed, current);
            var options = new InstanceValidatorParameters();
            if (request.has("profile") && !request.get("profile").isJsonNull()) {
                String profile = request.get("profile").getAsString();
                if (current.getContext().fetchResource(StructureDefinition.class, profile) == null)
                    throw new IllegalArgumentException();
                options.addProfile(profile);
            }
            var outcome = current.validate("instance.json", ByteProvider.forBytes(instance), FhirFormat.JSON, options, new ArrayList<>());
            if (!loaded(current).equals(strings(manifest.getAsJsonObject("contexts").getAsJsonArray(context))))
                throw new IllegalStateException("Loaded package mismatch after validation");
            var serialized = com.google.gson.JsonParser.parseString(new JsonParser().composeString(outcome));
            COMPLETED.incrementAndGet();
            send(exchange, 200, Map.of("snapshot_id", manifest.get("snapshot_id").getAsString(),
                "validator_sha256", manifest.get("validator_sha256").getAsString(), "terminology_mode", mode,
                "terminology_endpoint", endpoint == null ? "" : endpoint, "package", context,
                "loaded_packages", loaded(current), "outcome", serialized));
        } catch (IllegalArgumentException | NullPointerException | ClassCastException | JsonParseException e) {
            send(exchange, 400, Map.of("error", "Invalid request or unresolved profile"));
        } catch (Exception e) {
            if (locked && context != null) { ENGINES.remove(context); cached = List.copyOf(ENGINES.keySet()); }
            send(exchange, 503, Map.of("error", "Validation engine failed; retry explicitly"));
        } finally {
            if (deadline != null) deadline.cancel(false);
            if (locked) ENGINE_LOCK.unlock();
            ADMISSION.release();
            exchange.close();
        }
    }

    public static void main(String[] args) throws Exception {
        if (args.length > 0 && args[0].equals("health")) {
            var response = HttpClient.newHttpClient().send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:8080/health"))
                .timeout(java.time.Duration.ofSeconds(3)).GET().build(), HttpResponse.BodyHandlers.discarding());
            System.exit(response.statusCode() == 200 ? 0 : 1);
        }
        Path snapshots = Path.of("/snapshots");
        String selected = Files.readString(snapshots.resolve("current")).trim();
        if (!selected.matches("[0-9a-f]{64}")) throw new IllegalStateException("Invalid snapshot pointer");
        Path snapshot = snapshots.resolve(selected);
        Path home = Path.of("/cache").resolve(selected);
        System.setProperty("user.home", home.toString());
        manifest = com.google.gson.JsonParser.parseString(Files.readString(snapshot.resolve("manifest.json"))).getAsJsonObject();
        if (!sha256(Path.of("/app/validator.jar")).equals(manifest.get("validator_sha256").getAsString()))
            throw new IllegalStateException("Runtime checksum differs");
        for (var entry : manifest.getAsJsonObject("files").entrySet()) {
            Path source = snapshot.resolve(entry.getKey()).normalize();
            if (!source.startsWith(snapshot) || !sha256(source).equals(entry.getValue().getAsString()))
                throw new IllegalStateException("Snapshot checksum differs");
            Path target = home.resolve(entry.getKey());
            Files.createDirectories(target.getParent());
            if (!Files.exists(target) || !sha256(target).equals(entry.getValue().getAsString()))
                Files.copy(source, target, StandardCopyOption.REPLACE_EXISTING);
        }
        endpoint = System.getenv("TERMINOLOGY_ENDPOINT");
        if (endpoint != null && endpoint.isBlank()) endpoint = null;
        if ("true".equals(System.getenv("REQUIRE_ONLINE")) && endpoint == null)
            throw new IllegalStateException("Online service requires TERMINOLOGY_ENDPOINT");
        mode = endpoint == null ? "offline" : "online";
        if (endpoint != null && !endpoint.startsWith("https://")) throw new IllegalStateException("HTTPS endpoint required");
        Files.writeString(Path.of("/work/settings.json"), JSON.toJson(Map.of("prohibitNetworkAccess", endpoint == null, "ignoreDefaultPackageServers", true)));
        System.setProperty("fhir.settings.path", "/work/settings.json");
        ManagedWebAccess.loadFromFHIRSettings();
        var server = HttpServer.create(new InetSocketAddress(8080), 32);
        server.setExecutor(new ThreadPoolExecutor(12, 12, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(32)));
        server.createContext("/health", exchange -> {
            send(exchange, ready ? 200 : 503, Map.of("ready", ready, "snapshot_id", selected,
                "mode", mode, "cached_contexts", cached, "engine_builds", BUILDS.get(),
                "completed", COMPLETED.get(), "evictions", EVICTIONS.get(), "admitted", 9 - ADMISSION.availablePermits()));
            exchange.close();
        });
        server.createContext("/validate", Server::validate);
        server.start();
        var startup = WATCHDOG.schedule(() -> Runtime.getRuntime().halt(124), 150, TimeUnit.SECONDS);
        engine(manifest.get("default_package").getAsString());
        ready = true;
        startup.cancel(false);
        System.out.println("Validator ready: " + selected + " mode=" + mode);
    }
}
